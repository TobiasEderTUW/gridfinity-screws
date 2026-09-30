"""Label artwork, composed offline from files in generator/assets/.

A label is the part type's icon(s) followed by a line of text, laid out on the 52.9 x 10.1 mm
plate of a 1-wide Regular label from label.alch.shop. Nothing here talks to the internet:

  * The icons are the website's own icons-only Raised exports, one per part type
    (tools/download_label_assets.py). The site's license allows using exported label files
    and forbids extracting its source or iconography, so the icon outlines are recovered by
    sectioning those exports, exactly as the old pipeline sectioned whole labels.
  * The text is set here in HarmonyOS Sans SC Regular, the font the site uses, from Huawei's
    unmodified distribution (assets/fonts, with its license), including the font's GPOS kerning.
  * The layout reproduces what the site's exports show (measured on 116 of them): icons at
    their exported size, text 2.0 mm after the icons, text cap height 5.7 mm, the whole group
    centred; the text shrinks only when the group would come closer than 1.2 mm to the edge.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import trimesh
from shapely.affinity import scale as _scale, translate as _translate
from shapely.geometry import Polygon
from shapely.ops import unary_union

ASSETS = Path(__file__).resolve().parent / 'assets'
CATALOGUE_PATH = ASSETS / 'label_catalogue.json'
FONT_PATH = ASSETS / 'fonts' / 'HarmonyOS_Sans_SC_Regular.ttf'

PLATE_W = 52.9               # 1-wide Regular label, the size the icons were exported on
PLATE_H = 10.1
EXPORT_BASE_T = 0.30         # exported Raised label: plate thickness ...
EXPORT_RELIEF_T = 0.15       # ... plus relief; sectioned halfway up the relief
TEXT_CAP_H = 5.7             # the site's default text height (cap height, baseline to top)
ICON_TEXT_GAP = 2.0          # last icon ink to first text ink
EDGE_MARGIN = 1.2            # closest the icon + text group gets to the plate edge
CURVE_STEPS = 8              # line segments per font curve segment
ICON_H = 8.12                # height of the site's icons (64 of 67 types; wing and heat-set
                             # nut icons are shorter, 6.45 / 7.0) - the reference for label scale


# The four kinds of the original invoice, as label.alch.shop part types.
LEGACY_PART = {'SK': 'allen__countersunk', 'ZK': 'allen__socket',
               'LK': 'phillips__pan', '6kt': 'hex__hex'}


# ----------------------------------------------------------------------------- catalogue
@lru_cache(maxsize=1)
def catalogue() -> dict:
    return json.loads(CATALOGUE_PATH.read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def parts() -> dict[str, dict]:
    return {p['key']: p for p in catalogue()['parts']}


def part(key: str) -> dict:
    try:
        return parts()[key]
    except KeyError:
        raise ValueError(f'unknown part type {key!r}') from None


def part_name(key: str) -> str:
    """Human-readable name, e.g. 'Allen / Hex socket · Countersunk' or 'Nyloc nut'."""
    p = part(key)
    cat = catalogue()
    if p['category'] == 'bolt':
        drive = next(d['name'] for d in cat['drives'] if d['key'] == p['drive'])
        head = next(h for h in cat['heads'] if h['key'] == p['head'])
        wood = ' (wood)' if head['group'] == 'Wood screws' else ''
        return f"{drive} · {head['name']}{wood}"
    nut = next(n for n in cat['nuts'] if n['key'] == p['nut'])
    noun = 'washer' if nut['group'] == 'Washers' else 'nut'
    return f"{nut['name']} {noun}"


# ----------------------------------------------------------------------------- STL sections
def orient_label_mesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Orient an exported label STL as X = label width, Y = label height, Z = thickness."""
    mesh = mesh.copy()
    ext = mesh.extents
    z_axis = int(np.argmin(ext))
    x_axis = int(np.argmax(ext))
    y_axis = ({0, 1, 2} - {z_axis, x_axis}).pop()
    mesh.vertices = mesh.vertices[:, [x_axis, y_axis, z_axis]]
    mesh.vertices[:, 2] -= mesh.bounds[0, 2]
    return mesh


def _signed_area(coords: np.ndarray) -> float:
    x = coords[:, 0]; y = coords[:, 1]
    return 0.5 * float(np.sum(x[:-1]*y[1:] - x[1:]*y[:-1]))


def section_loops(mesh: trimesh.Trimesh, z: float) -> list[np.ndarray]:
    section = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    if section is None:
        raise RuntimeError(f'No section found in label mesh at z={z:.3f}')
    # Pass the projection explicitly: to_planar() otherwise fits its own plane, and the
    # fitted normal can come back as -Z, which mirrors the artwork without any error.
    to_2d = np.array([[1.0, 0.0, 0.0, 0.0],
                      [0.0, 1.0, 0.0, 0.0],
                      [0.0, 0.0, 1.0, -z],
                      [0.0, 0.0, 0.0, 1.0]])
    planar, _ = section.to_planar(to_2D=to_2d)
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
        raise RuntimeError('label section contained no closed graphic loops')
    return loops


def nest_loops(loops) -> list[Polygon]:
    """Closed loops -> polygons with holes, by containment depth (even = solid)."""
    polys = []
    for arr in loops:
        p = Polygon(arr)
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty:
            continue
        polys.extend(p.geoms if p.geom_type == 'MultiPolygon' else [p])
    polys = [p for p in polys if p.area > 1e-5]
    reps = [p.representative_point() for p in polys]
    depth = [sum(1 for j, q in enumerate(polys) if i != j and q.area > p.area and q.contains(reps[i]))
             for i, p in enumerate(polys)]
    result = []
    for i, p in enumerate(polys):
        if depth[i] % 2:
            continue
        holes = [list(h.exterior.coords) for j, h in enumerate(polys)
                 if depth[j] == depth[i] + 1 and p.contains(reps[j])]
        result.append(Polygon(list(p.exterior.coords), holes))
    return result


def export_polygons(stl: Path) -> list[Polygon]:
    """Raised artwork of an exported label, sectioned halfway up the relief."""
    mesh = trimesh.load_mesh(stl, force='mesh')
    if mesh.is_empty:
        raise RuntimeError(f'empty label export: {stl}')
    mesh = orient_label_mesh(mesh)
    total = float(mesh.extents[2])
    expected = EXPORT_BASE_T + EXPORT_RELIEF_T
    if not (expected - 0.10 <= total <= expected + 0.12):
        raise RuntimeError(f'{stl.name}: label export is {total:.3f} mm thick, expected about '
                           f'{expected:.2f} mm; the asset may not be a Raised export')
    # Section every solid on its own and union afterwards: some icons (the wood-screw threads)
    # are overlapping strokes, and nesting all loops together by even/odd would turn their
    # overlaps into holes.
    z = EXPORT_BASE_T + EXPORT_RELIEF_T / 2
    polys = []
    for body in mesh.split(only_watertight=False):
        if body.bounds[0][2] < z < body.bounds[1][2]:
            polys += nest_loops(section_loops(body, z))
    return _disjoint(polys)


@lru_cache(maxsize=None)
def icon_polygons(key: str) -> tuple[Polygon, ...]:
    """The part type's icon(s), as exported: centred on the plate, 8.1 mm high."""
    return tuple(export_polygons(ASSETS / part(key)['icon_file']))


# ----------------------------------------------------------------------------- text
@lru_cache(maxsize=1)
def _font():
    from fontTools.ttLib import TTFont
    return TTFont(str(FONT_PATH))


@lru_cache(maxsize=1)
def _kerning():
    """GPOS 'kern' pair adjustments as a lookup function (left glyph, right glyph) -> units.
    Like the site's text engine, the first subtable whose coverage holds the left glyph
    decides; format 1 falls through when the pair is not listed."""
    font = _font()
    if 'GPOS' not in font:
        return lambda a, b: 0
    gpos = font['GPOS'].table
    idx = sorted({i for fr in gpos.FeatureList.FeatureRecord if fr.FeatureTag == 'kern'
                  for i in fr.Feature.LookupListIndex})
    subtables = []
    for i in idx:
        lookup = gpos.LookupList.Lookup[i]
        for st in lookup.SubTable:
            if lookup.LookupType == 9:
                if st.ExtensionLookupType != 2:
                    continue
                st = st.ExtSubTable
            elif lookup.LookupType != 2:
                continue
            subtables.append(st)

    def value(v) -> int:
        return int(getattr(v, 'XAdvance', 0) or 0) if v is not None else 0

    def kern(a: str, b: str) -> int:
        for st in subtables:
            cov = st.Coverage.glyphs
            if a not in cov:
                continue
            if st.Format == 1:
                pairs = st.PairSet[cov.index(a)].PairValueRecord
                for rec in pairs:
                    if rec.SecondGlyph == b:
                        return value(rec.Value1)
                continue
            c1 = st.ClassDef1.classDefs.get(a, 0) if st.ClassDef1 else 0
            c2 = st.ClassDef2.classDefs.get(b, 0) if st.ClassDef2 else 0
            return value(st.Class1Record[c1].Class2Record[c2].Value1)
        return 0
    return kern


class _FlattenPen:
    """fontTools pen that turns outlines into closed point lists."""
    def __init__(self, glyph_set):
        from fontTools.pens.basePen import BasePen

        outer = self

        class Pen(BasePen):
            def _moveTo(self, p):
                outer.cur = [p]

            def _lineTo(self, p):
                outer.cur.append(p)

            def _curveToOne(self, p1, p2, p3):
                p0 = outer.cur[-1]
                for t in np.linspace(0, 1, CURVE_STEPS + 1)[1:]:
                    u = 1 - t
                    outer.cur.append((u**3*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0],
                                      u**3*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1]))

            def _qCurveToOne(self, p1, p2):
                p0 = outer.cur[-1]
                for t in np.linspace(0, 1, CURVE_STEPS + 1)[1:]:
                    u = 1 - t
                    outer.cur.append((u*u*p0[0] + 2*u*t*p1[0] + t*t*p2[0],
                                      u*u*p0[1] + 2*u*t*p1[1] + t*t*p2[1]))

            def _closePath(self):
                if len(outer.cur) >= 3:
                    outer.loops.append(outer.cur)
                outer.cur = []

            _endPath = _closePath

        self.loops: list[list[tuple[float, float]]] = []
        self.cur: list = []
        self.pen = Pen(glyph_set)


def text_polygons(text: str) -> list[Polygon]:
    """The text in font units, pen starting at x = 0 on the baseline, kerned."""
    font = _font()
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()
    hmtx = font['hmtx']
    kern = _kerning()
    names = []
    for ch in text:
        name = cmap.get(ord(ch))
        if name is None:
            raise ValueError(f'the label font has no glyph for {ch!r} (U+{ord(ch):04X})')
        names.append(name)
    loops = []
    x = 0.0
    for i, name in enumerate(names):
        fp = _FlattenPen(glyph_set)
        glyph_set[name].draw(fp.pen)
        for loop in fp.loops:
            arr = np.asarray(loop, dtype=float)
            arr[:, 0] += x
            loops.append(np.vstack([arr, arr[:1]]))
        x += hmtx[name][0]
        if i + 1 < len(names):
            x += kern(name, names[i + 1])
    return nest_loops(loops) if loops else []


def _bounds(polys) -> tuple[float, float, float, float]:
    b = np.array([p.bounds for p in polys])
    return float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 2].max()), float(b[:, 3].max())


# ----------------------------------------------------------------------------- composition
def _disjoint(polys) -> list[Polygon]:
    """Union into non-overlapping polygons. Some icons (the wood-screw threads) are drawn as
    overlapping strokes; extruded as they are, they make OCC booleans explode."""
    merged = unary_union([p if p.is_valid else p.buffer(0) for p in polys if not p.is_empty])
    geoms = getattr(merged, 'geoms', [merged])
    return [g for g in geoms if g.geom_type == 'Polygon' and g.area > 1e-4]


def compose(key: str, text: str, site_layout: bool = True) -> list[Polygon]:
    """Icons + text, centred at the origin, in the site's plate units (mm on a 1-wide label).

    site_layout True: the site's own layout on the 52.9 mm plate (a very long text shrinks
    when the group would come within 1.2 mm of the edge) - what the exports are checked against.
    site_layout False: the natural layout, text always at full cap height, never shrunk or
    condensed. The printed labels use this and scale the whole label instead
    (generator: label_scales), so every label of a box width is exactly alike.
    """
    return _disjoint(_compose(key, text, site_layout))


@lru_cache(maxsize=4096)
def natural_width(key: str, text: str) -> float:
    """Width of the natural layout (site_layout=False), in plate units."""
    x0, _y0, x1, _y1 = _bounds(compose(key, text, site_layout=False))
    return x1 - x0


def _compose(key: str, text: str, site_layout: bool = True) -> list[Polygon]:
    icons = list(icon_polygons(key))
    text = (text or '').strip()
    glyphs = text_polygons(text) if text else []
    if not glyphs:
        return icons
    # The site centres the icons' layout box, not their ink, and a drive icon can be
    # narrower than its slot (External hex: 7.0 of 8.1 mm). The icons-only export is that
    # box centred at x = 0, so the box is symmetric about 0. For screws its right edge is the
    # head icon's ink (the head sits flush in its box); the drive ink may miss or overshoot its
    # slot (Phillips: 0.02 mm). A nut is a single centred icon.
    ix0, _iy0, ix1, _iy1 = _bounds(icons)
    half = ix1 if part(key)['category'] == 'bolt' else max(-ix0, ix1)
    ix0, icons_w = -half, 2 * half

    tx0, ty0, tx1, ty1 = _bounds(glyphs)
    sx = sy = TEXT_CAP_H / ty1                # cap height = ink top above the baseline
    if site_layout:
        budget = PLATE_W - 2 * EDGE_MARGIN - icons_w - ICON_TEXT_GAP
        if (tx1 - tx0) * sx > budget:
            sx = sy = budget / (tx1 - tx0)
    text_w = (tx1 - tx0) * sx
    group_l = -(icons_w + ICON_TEXT_GAP + text_w) / 2

    out = [_translate(p, group_l - ix0, 0) for p in icons]
    x_off = group_l + icons_w + ICON_TEXT_GAP
    y_mid = (ty0 + ty1) / 2 * sy              # ink centred vertically, like the exports
    for g in glyphs:
        g = _scale(g, sx, sy, origin=(0, 0))
        out.append(_translate(g, x_off - tx0 * sx, -y_mid))
    return out


def default_text(key: str, thread: str, length: int | None) -> str:
    """'M6×20' for screws and bolts, 'M6' for nuts and washers."""
    if part(key)['category'] == 'bolt' and length:
        return f'{thread}×{length:g}'
    return thread


def svg_path(polys, precision: int = 2) -> str:
    """SVG path data (y up flipped to y down), even-odd."""
    parts_ = []
    for p in polys:
        for ring in [p.exterior, *p.interiors]:
            pts = list(ring.coords)[:-1]
            parts_.append('M' + 'L'.join(f'{x:.{precision}f} {-y:.{precision}f}' for x, y in pts) + 'Z')
    return ''.join(parts_)


# ----------------------------------------------------------------------------- editor catalogue
def _icon_box(key: str) -> tuple[float, float]:
    """Half width and height of the icons' layout box (see compose())."""
    x0, y0, x1, y1 = _bounds(icon_polygons(key))
    half = x1 if part(key)['category'] == 'bolt' else max(-x0, x1)
    return half, y1 - y0


WEB_SIMPLIFY = 0.02            # mm; editor icons only, the print geometry is never simplified


def web_catalogue() -> dict:
    """Everything the editor needs to pick part types and draw labels, derived from the
    exports: names, drive/head compatibility, SVG icons (mm, y down, plate centre = 0)."""
    cat = catalogue()

    def svg_path(polys):
        return globals()['svg_path']([q.simplify(WEB_SIMPLIFY) for q in polys])
    drive_icon: dict[str, str] = {}
    head_icon: dict[str, str] = {}
    out_parts = []
    for key, p in parts().items():
        polys = list(icon_polygons(key))
        half, height = _icon_box(key)
        entry = {'key': key, 'category': p['category'], 'name': part_name(key),
                 'icon': svg_path(polys), 'half': round(half, 3)}
        if p['category'] == 'bolt':
            entry.update(drive=p['drive'], head=p['head'])
            split = -half + height + 0.8          # drive slot is as wide as the icons are high
            left = [q for q in polys if q.centroid.x < split]
            right = [q for q in polys if q.centroid.x >= split]
            drive_icon.setdefault(p['drive'], svg_path([_translate(q, half - height / 2, 0) for q in left]))
            hx = (_bounds(right)[0] + _bounds(right)[2]) / 2       # centred for the picker
            head_icon.setdefault(p['head'], svg_path([_translate(q, -hx, 0) for q in right]))
        else:
            entry['nut'] = p['nut']
        out_parts.append(entry)
    return {
        'source': cat['source'], 'site_version': cat['site_version'], 'downloaded': cat['downloaded'],
        'layout': {'plate_w': PLATE_W, 'plate_h': PLATE_H, 'cap_h': TEXT_CAP_H, 'gap': ICON_TEXT_GAP,
                   'edge': EDGE_MARGIN, 'icon_h': ICON_H,
                   'font_cap_ratio': _font()['OS/2'].sCapHeight / _font()['head'].unitsPerEm},
        'drives': [{**d, 'icon': drive_icon.get(d['key'], '')} for d in cat['drives']],
        'heads': [{**h, 'icon': head_icon.get(h['key'], '')} for h in cat['heads']],
        'nuts': [{**n, 'icon': next(e['icon'] for e in out_parts if e['key'] == n['key'])} for n in cat['nuts']],
        'drive_heads': cat['drive_heads'],
        'parts': out_parts,
        'legacy': LEGACY_PART,
    }


if __name__ == '__main__':
    import argparse
    import sys
    ap = argparse.ArgumentParser(description='Write the editor part catalogue (web/public/parts.json).')
    ap.add_argument('target', nargs='?', default=str(Path(__file__).resolve().parents[1] / 'web' / 'public' / 'parts.json'))
    target = Path(ap.parse_args().target)
    target.write_text(json.dumps(web_catalogue(), ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'{target}: {target.stat().st_size // 1024} KiB', file=sys.stderr)

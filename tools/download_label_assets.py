#!/usr/bin/env python3
"""One-off developer tool: download the label icon assets the generator composes labels from.

Not part of the service. The api never talks to the internet; everything it needs is in
generator/assets/ and this script is how that folder was filled. Run it again only when
label.alch.shop adds part types.

What it downloads, and why only this:
  * One *icons-only* Raised label per part type (text box empty), exported through the
    website's own STL button: 53 drive/head combinations and 14 nuts/washers.
    The site's LICENSE (ALCH AS, proprietary) permits using the label files it exports
    and forbids extracting its source or iconography, so the icons come from exports,
    never from the page source.
  * The part catalogue (keys, display names, groups, which heads each drive allows),
    read from the visible form controls.

The label text is not downloaded: the generator sets it itself in HarmonyOS Sans SC
Regular, the font the site uses, installed from Huawei's own package
(generator/assets/fonts/, see the LICENSE there).

Usage (Playwright + Chromium needed, e.g. inside the api image before they were dropped,
or `uv run --with playwright`):
    python tools/download_label_assets.py [--out generator/assets] [--only allen__countersunk,...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

URL = "https://label.alch.shop/"
WIDTH = "1"          # label width setting; the icons do not depend on it except at 0.5


def _set_select(page, sel_id: str, value: str) -> None:
    """The native selects are hidden behind icon pickers; set them the way a pick does."""
    page.evaluate(
        """([id, v]) => { const s = document.getElementById(id); s.value = v;
             s.dispatchEvent(new Event('input', {bubbles: true}));
             s.dispatchEvent(new Event('change', {bubbles: true})); }""",
        [sel_id, value])


def _toggle(page, group_id: str, value: str) -> None:
    page.locator(f'#{group_id} .toggle-opt[data-val="{value}"]').click()


def _options(page, sel_id: str) -> list[dict]:
    return page.evaluate(
        """id => [...document.getElementById(id).options].map(o => ({
             key: o.value, name: o.textContent.trim(), disabled: o.disabled,
             group: o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : null}))""",
        sel_id)


def _download(page, target: Path) -> None:
    btn = page.get_by_role('button', name='STL', exact=True)
    with page.expect_download(timeout=60_000) as info:
        btn.first.click()
    info.value.save_as(str(target))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=str(Path(__file__).resolve().parents[1] / 'generator' / 'assets'))
    ap.add_argument('--only', default='', help='comma-separated part keys to (re)download')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    icon_dir = out / 'label_icons'
    icon_dir.mkdir(parents=True, exist_ok=True)
    only = {k for k in args.only.split(',') if k}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(accept_downloads=True)
        page.goto(URL, wait_until='networkidle', timeout=120_000)
        version = page.evaluate("() => (document.body.innerText.match(/v\\d+\\.\\d+\\.\\d+/) || [''])[0]")

        _toggle(page, 'ltypeToggle', 'regular')
        page.evaluate(f"""() => {{ const r = document.getElementById('lwidth'); r.value = '{WIDTH}';
                          r.dispatchEvent(new Event('input', {{bubbles: true}})); }}""")
        _toggle(page, 'styleToggle', 'raised')
        if not page.locator('#showIcons').is_checked():
            page.locator('#showIcons').check()
        page.fill('#text', '')
        page.dispatch_event('#text', 'input')

        # ---- catalogue ----
        _toggle(page, 'categoryToggle', 'bolts')
        drives = [{k: o[k] for k in ('key', 'name', 'group')} for o in _options(page, 'drive')]
        heads = [{k: o[k] for k in ('key', 'name', 'group')} for o in _options(page, 'head')]
        allowed: dict[str, list[str]] = {}
        for d in drives:
            _set_select(page, 'drive', d['key'])
            page.wait_for_timeout(100)
            allowed[d['key']] = [o['key'] for o in _options(page, 'head') if not o['disabled']]
        _toggle(page, 'categoryToggle', 'nuts')
        nuts = [{k: o[k] for k in ('key', 'name', 'group')} for o in _options(page, 'nut')]

        parts = []
        for d in drives:
            for h in allowed[d['key']]:
                parts.append({'key': f"{d['key']}__{h}", 'category': 'bolt', 'drive': d['key'], 'head': h})
        for n in nuts:
            parts.append({'key': n['key'], 'category': 'nut', 'nut': n['key']})

        # ---- icons-only exports ----
        todo = [pt for pt in parts if not only or pt['key'] in only]
        for i, pt in enumerate(todo, 1):
            if pt['category'] == 'bolt':
                _toggle(page, 'categoryToggle', 'bolts')
                _set_select(page, 'drive', pt['drive'])
                _set_select(page, 'head', pt['head'])
                got = page.evaluate("() => [document.getElementById('drive').value, document.getElementById('head').value]")
                if got != [pt['drive'], pt['head']]:
                    raise RuntimeError(f"{pt['key']}: the site switched the selection to {got}")
            else:
                _toggle(page, 'categoryToggle', 'nuts')
                _set_select(page, 'nut', pt['nut'])
            page.wait_for_timeout(250)
            target = icon_dir / f"{pt['key']}.stl"
            _download(page, target)
            pt['icon_file'] = f"label_icons/{target.name}"
            print(f"[{i:02d}/{len(todo)}] {target.name}  {target.stat().st_size // 1024} KiB", flush=True)
        browser.close()

    for pt in parts:
        pt.setdefault('icon_file', f"label_icons/{pt['key']}.stl")
    catalogue = {
        'source': URL, 'site_version': version,
        'downloaded': time.strftime('%Y-%m-%d'),
        'export_settings': {'box_type': 'regular', 'width': float(WIDTH), 'style': 'raised',
                            'show_icons': True, 'text': ''},
        'drives': drives, 'heads': heads, 'nuts': nuts,
        'drive_heads': allowed,
        'parts': parts,
    }
    (out / 'label_catalogue.json').write_text(json.dumps(catalogue, indent=1, ensure_ascii=False) + '\n',
                                              encoding='utf-8')
    print(f"{len(parts)} part types, catalogue -> {out / 'label_catalogue.json'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

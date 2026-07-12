#!/usr/bin/env python3
"""EyeToSpec — a local visual tool that turns where-your-eye-wants-things into a
precise coordinate JSON for AI coding agents.

Single entry point, Python standard library only. No pip install, no build step.

    python3 serve.py                # scan ./config, open the browser
    python3 serve.py --port 8771    # use another port
    python3 serve.py --no-open      # don't auto-open the browser

The server does exactly three things a static page cannot:
  1. GET  /api/packs            -> list the asset packs found in ./config
  2. GET  /api/pack/<id>        -> one pack's manifest (pack.json)
  3. POST /api/save/<id>        -> write the exported coordinates to ./output/<id>.json

Everything else (the drag surface, normalization, export) happens in the browser.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs, quote

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
OUTPUT_DIR = os.path.join(ROOT, "output")
WEB_DIR = os.path.join(ROOT, "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def content_type_for(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _read_pack_entry(pack_id, pack_dir):
    """Build a pack list entry from a dir holding a pack.json OR a source.json
    (live game-config pointer). Returns None if neither is readable."""
    manifest_path = os.path.join(pack_dir, "pack.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    else:
        source = read_source(pack_dir)
        if not source:
            return None
        try:
            data, _ = build_source_manifest(source)
        except (OSError, json.JSONDecodeError):
            return None
        data["live"] = True
    has_export = os.path.isfile(os.path.join(OUTPUT_DIR, pack_id + ".json"))
    return {
        "id": pack_id,
        "name": data.get("name", pack_id),
        "description": data.get("description", ""),
        "elementCount": len(data.get("elements", [])),
        "exported": has_export,
        "live": bool(data.get("live")),
    }


def read_group(group_dir):
    """Read an optional group.json (name/description/order) from a group dir."""
    path = os.path.join(group_dir, "group.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Source-backed packs: a pack dir may hold a source.json instead of a pack.json.
# It points at a game layout config (the SINGLE SOURCE OF TRUTH) which EyeToSpec
# reads LIVE and converts to a pack manifest at request time — zero copy, zero
# drift. The game's keyed-object layout ({avatar:{cx,cy,w},...}) becomes
# elements:[{id,cx,cy,w,...}]; each element's `tex` (texture key) is resolved to
# a real file via the repo's asset-profiles.json + resourceRoot, both named in
# the config's own `_eyetospec` block.
# ---------------------------------------------------------------------------

# Top-level layout keys that are page metadata, not placeable elements.
# `fitMode` (elastic|scroll, spec 2026-07-12) is an ADAPTATION field: it tells the
# game how the page reflows on a taller/shorter screen. EyeToSpec is a pure static
# compositor and does NOT act on it — listed here only so it's skipped, never drawn.
_LAYOUT_META_KEYS = {"_comment", "_eyetospec", "mode", "fitMode", "elasticZone",
                     "baselineRatio", "bg"}
# Element fields passed through verbatim into the manifest.
_ELEM_PASS = ("cx", "cy", "w", "h", "anchor", "rotation", "text", "color",
              "align", "fontSize", "fontFamily", "fontWeight", "stroke",
              "strokeWidth", "shadow", "fill", "alpha", "radius")

# --- baseline-layout projection (mode == "baseline-layout") -----------------
# The home page is a baseline-layout config: elements carry NO literal `cy`, they
# carry an offset against one of three anchor groups (center → baselineRatio,
# top → safe-area top, bottom → screen bottom). The editor only knows literal
# normalized cy, so we PROJECT each single-point element's offset into a cy for
# display/drag, and INVERT it on save. Because the pack canvas height is chosen
# to equal the game bg width-locked display height, originY/H == baselineRatio
# and every offset (a fraction of H) maps to a cy by simple add/subtract — no
# screen-height term. The draggable baseline (anchorLine.cy) seeds to
# baselineRatio: dragging it re-pins the hen AND marks the bg nest (they converge
# in this matched space), and on save writes back baselineRatio + bg.anchorY.
# Single-point elements (one literal `cx`); multi-point rows and equal-spaced
# arrays are handled separately below.
_BASELINE_CENTER = ("hen", "banner", "chickZone")  # cy = baselineRatio + offsetY
_BASELINE_TOP = ("avatar", "res_heart_icon", "res_star_icon", "res_egg_icon")  # cy = offsetTop
_BASELINE_BOTTOM = ("start",)                 # cy = 1 - offsetBottom

# Multi-point rows: one layout key holds SEVERAL x fields sharing one row y (an
# offset* against an anchor group). Each x field becomes its own draggable editor
# element (id "<row>.<field>"), so the owner places each icon on the real bg. On
# save, each child's cx writes back its x field and its cy re-derives the row's
# shared offset. `group` picks the y projection (top → offsetTop, bottom →
# 1-offsetBottom). `points` maps x-field → texture key (None = placeholder box).
_BASELINE_ROWS = {
    "arrows": {"group": "bottom", "offset": "offsetBottom",
               "points": [("leftX", "home-arrow-left"), ("rightX", "home-arrow-right")]},
    "tabs": {"group": "bottom", "offset": "offsetBottom",
             "points": [("shopX", "home-tab-shop"), ("henhouseX", "home-tab-henhouse"),
                        ("towerX", "home-tab-tower"), ("dungeonX", "home-tab-dungeon")]},
}
# Separator between a row key and its x field in a child element id.
_ROW_SEP = "."

# Equal-spaced arrays: one layout key renders N icons at cx + (i - (N-1)/2)*gap,
# sharing one center `cx`, one `gap`, one `size` (all fractions of width). Unlike
# _BASELINE_ROWS (independent x per point), these move as a set: dragging the
# MIDDLE child re-centers the row (writes cx); dragging an END child re-derives
# the spacing (writes gap). Each child id is "<key>.<i>" (1-based). `tex` is a
# format string over the 1-based index for per-slot art (home-reward-1-closed …).
_BASELINE_ARRAYS = {
    "chests": {"group": "bottom", "offset": "offsetBottom", "count": 3,
               "tex": "home-reward-%d-closed"},
}


def _row_offset_to_cy(spec, v, baseline_ratio):
    """The shared editor cy for a multi-point row from its offset field."""
    off = _num(v.get(spec["offset"]), 0.0)
    return off if spec["group"] == "top" else 1.0 - off


def _project_baseline_row(key, v, baseline_ratio, profiles):
    """Explode a multi-point row into editor child elements (one per x field).
    Each child id is "<row>.<field>"; cy is the row's shared projected y; w
    reuses the row's `size` (a fraction of width) so icons show at true scale."""
    spec = _BASELINE_ROWS[key]
    cy = _row_offset_to_cy(spec, v, baseline_ratio)
    size = v.get("size", v.get("w"))
    out = []
    for field, tex in spec["points"]:
        if field not in v:
            continue
        el = {"id": key + _ROW_SEP + field, "cx": v.get(field), "cy": cy}
        if isinstance(size, (int, float)):
            el["w"] = size
        if tex and tex in profiles:
            scene, fmt = profiles[tex]
            el["file"] = "%s/%s.%s" % (scene, tex, fmt)
        out.append(el)
    return out


def _project_baseline_array(key, v, baseline_ratio, profiles):
    """Explode an equal-spaced array into N editor children at cx+(i-(N-1)/2)*gap.
    Child id "<key>.<i>" (1-based); shared cy; w = the array's `size`."""
    spec = _BASELINE_ARRAYS[key]
    cy = _row_offset_to_cy(spec, v, baseline_ratio)
    count = spec["count"]
    cx = _num(v.get("cx"), 0.5)
    gap = _num(v.get("gap"), 0.0)
    size = v.get("size", v.get("w"))
    out = []
    for i in range(1, count + 1):
        center = cx + (i - 1 - (count - 1) / 2.0) * gap
        el = {"id": "%s%s%d" % (key, _ROW_SEP, i), "cx": center, "cy": cy}
        if isinstance(size, (int, float)):
            el["w"] = size
        tex = spec["tex"] % i
        if tex in profiles:
            scene, fmt = profiles[tex]
            el["file"] = "%s/%s.%s" % (scene, tex, fmt)
        out.append(el)
    return out


def _invert_baseline_arrays(layout, edited):
    """Fold equal-spaced array children ("<key>.<i>") back into the layout's
    shared cx (center) + gap (spacing) + offset. Needs ALL children at once:
    gap = (last.cx - first.cx)/(count-1); cx = mean of child centers; offset from
    any child's cy. Children with a missing/partial set are folded best-effort."""
    for key, spec in _BASELINE_ARRAYS.items():
        row = layout.get(key)
        if not isinstance(row, dict):
            continue
        prefix = key + _ROW_SEP
        kids = []
        for eid, el in edited.items():
            if not eid.startswith(prefix):
                continue
            idx = eid[len(prefix):]
            if idx.isdigit() and "cx" in el:
                kids.append((int(idx), el))
        if not kids:
            continue
        kids.sort(key=lambda p: p[0])
        xs = [float(el["cx"]) for _, el in kids]
        # center = mean of the child centers (exact for a symmetric equal-spaced set).
        row["cx"] = round(sum(xs) / len(xs), 4)
        # spacing from the span between the extreme children.
        if len(kids) >= 2:
            span_i = kids[-1][0] - kids[0][0]
            if span_i > 0:
                row["gap"] = round((xs[-1] - xs[0]) / span_i, 4)
        # shared offset from any child's cy.
        cy = next((float(el["cy"]) for _, el in kids if isinstance(el.get("cy"), (int, float))), None)
        if cy is not None:
            off = cy if spec["group"] == "top" else 1.0 - cy
            row[spec["offset"]] = round(off, 4)


def _project_baseline_element(key, v, baseline_ratio):
    """Project one baseline element {cx, offset*} → editor {cx, cy, w}. Returns
    None for elements without a single-point cx (edge-pinned rows are skipped)."""
    if "cx" not in v:
        return None
    if key in _BASELINE_CENTER:
        cy = baseline_ratio + _num(v.get("offsetY"), 0.0)
    elif key in _BASELINE_TOP:
        cy = _num(v.get("offsetTop"), 0.0)
    elif key in _BASELINE_BOTTOM:
        cy = 1.0 - _num(v.get("offsetBottom"), 0.0)
    else:
        return None
    return cy


def _invert_baseline_element(key, cy, baseline_ratio):
    """Inverse of _project_baseline_element: editor cy → the game's offset field
    as (field_name, value). Returns None if the key isn't a baseline element."""
    if key in _BASELINE_CENTER:
        return ("offsetY", cy - baseline_ratio)
    if key in _BASELINE_TOP:
        return ("offsetTop", cy)
    if key in _BASELINE_BOTTOM:
        return ("offsetBottom", 1.0 - cy)
    return None


def _num(v, d):
    return v if isinstance(v, (int, float)) else d


# --- top-elastic projection (mode == "top-elastic") -------------------------
# The loadout BASE pack (resolveLoadoutBaseLayout): a topbar + a deploy row of N
# slots generated from a cx[] array, all anchored to the safe-area TOP. Editor cy
# == offsetTop directly (the safe band is a review overlay the editor draws
# separately, NOT subtracted — same convention as the baseline TOP group). Deploy
# inner parts (portrait/remove/plus) are dx/dy offsets from a slot center (dx a
# fraction of W, dy a fraction of H), SHARED across all slots, so we project them
# once anchored to slot 1 and re-derive dx/dy from that slot on save.
_TOP_ELASTIC_TOPBAR = ("back", "board", "title", "resHeart", "resStar", "resEgg")
# (part-key, writes-own-w). portrait carries no w in the schema (it uses
# portraitScale × the live tower width at runtime); we give it a placeholder
# display box on projection but never write a w back.
_TOP_ELASTIC_DEPLOY_PARTS = (("portrait", False), ("remove", True), ("plus", True))


def _resolve_file(el, tex, profiles):
    """Attach the resolved asset file to an element if its tex is known."""
    if tex and tex in profiles:
        scene, fmt = profiles[tex]
        el["file"] = "%s/%s.%s" % (scene, tex, fmt)


def _project_flat_elements(layout, profiles):
    """Project a FLAT top-elastic layout (shop/henhouse/challenge) → elements.

    These pages carry literal top-level cx/cy per key (topbar_back, res_*_icon,
    plank_1…), NOT the loadout's nested topbar/deploy schema. Same math as the
    generic flat loop in build_source_manifest: pass fields verbatim, resolve tex."""
    out = []
    for key, v in layout.items():
        if key in _LAYOUT_META_KEYS or not isinstance(v, dict):
            continue
        if "cx" not in v or "cy" not in v:
            continue
        el = {"id": key}
        for fld in _ELEM_PASS:
            if fld in v:
                el[fld] = v[fld]
        _resolve_file(el, v.get("tex"), profiles)
        out.append(el)
    return out


def _project_top_elastic(layout, profiles):
    """Project the loadout BASE pack → draggable editor elements.

    topbar.<name>       cx=cx,           cy=offsetTop,        w=w
    deploy.slot.<i>     cx=cx[i-1],      cy=offsetTop,        w=deploy.w   (1-based)
    deploy.<part>       cx=cx[0]+dx,     cy=offsetTop+dy,     w=part.w

    Mirrors resolveLoadoutBaseLayout at editor scale (safeArea.top=0).

    Flat pages (shop/henhouse/challenge) reuse mode "top-elastic" but have NO
    nested topbar/deploy dicts — fall back to the flat projection for them."""
    if not isinstance(layout.get("topbar"), dict) and not isinstance(layout.get("deploy"), dict):
        return _project_flat_elements(layout, profiles)
    out = []
    topbar = layout.get("topbar")
    if isinstance(topbar, dict):
        for name in _TOP_ELASTIC_TOPBAR:
            e = topbar.get(name)
            if not isinstance(e, dict) or "cx" not in e:
                continue
            el = {"id": "topbar" + _ROW_SEP + name,
                  "cx": _num(e.get("cx"), 0.5),
                  "cy": _num(e.get("offsetTop"), 0.0)}
            if isinstance(e.get("w"), (int, float)):
                el["w"] = e["w"]
            _resolve_file(el, e.get("tex"), profiles)
            out.append(el)
    dep = layout.get("deploy")
    if isinstance(dep, dict):
        cx_arr = dep.get("cx")
        off_top = _num(dep.get("offsetTop"), 0.0)
        dep_w = dep.get("w")
        slot_tex = dep.get("slotTex")
        if isinstance(cx_arr, list) and cx_arr:
            for i, cx in enumerate(cx_arr, start=1):
                el = {"id": "deploy" + _ROW_SEP + "slot" + _ROW_SEP + str(i),
                      "cx": _num(cx, 0.5), "cy": off_top}
                if isinstance(dep_w, (int, float)):
                    el["w"] = dep_w
                _resolve_file(el, slot_tex, profiles)  # 5 slots share one slotTex
                out.append(el)
            # inner parts anchored to slot 1 (first) center
            anchor_cx = _num(cx_arr[0], 0.5)
            pscale = _num(dep.get("portraitScale"), 1.0)
            for part, _has_w in _TOP_ELASTIC_DEPLOY_PARTS:
                pv = dep.get(part)
                if not isinstance(pv, dict):
                    continue
                el = {"id": "deploy" + _ROW_SEP + part,
                      "cx": anchor_cx + _num(pv.get("dx"), 0.0),
                      "cy": off_top + _num(pv.get("dy"), 0.0)}
                if isinstance(pv.get("w"), (int, float)):
                    el["w"] = pv["w"]
                elif part == "portrait" and isinstance(dep_w, (int, float)):
                    el["w"] = round(dep_w * pscale, 6)  # placeholder display box
                _resolve_file(el, pv.get("tex") or pv.get("calibTex"), profiles)
                out.append(el)
    return out


def _invert_flat_elements(layout, edited):
    """Fold edited elements back into a FLAT top-elastic layout (shop/henhouse/
    challenge). The inverse of _project_flat_elements: for each edited element,
    update the matching top-level key's placement + text fields in place; delete
    keys the editor removed (soft-delete → dropped from the manifest); materialize
    duplicated keys (recover `tex` from the file basename). Preserves tex/metadata
    on untouched keys — never rewrites the whole schema."""
    # Delete: any placeable element key present in the layout but absent from the
    # edited manifest was soft-deleted in the editor (buildPackManifest drops it).
    existing = [k for k, v in layout.items()
                if k not in _LAYOUT_META_KEYS and isinstance(v, dict)
                and "cx" in v and "cy" in v]
    for k in existing:
        if k not in edited:
            del layout[k]

    for key, el in edited.items():
        if key in _LAYOUT_META_KEYS:
            continue
        target = layout.get(key)
        if not isinstance(target, dict):
            # Duplicated element the source doesn't have yet. The manifest carries
            # `file` (scene/tex.fmt) not `tex`, so recover the game's tex key.
            target = {}
            file = el.get("file")
            if isinstance(file, str) and file:
                target["tex"] = os.path.splitext(os.path.basename(file))[0]
            layout[key] = target
        for fld in _ELEM_PASS:
            if fld in el and el[fld] is not None:
                v = el[fld]
                if fld in ("cx", "cy", "w", "h") and isinstance(v, (int, float)):
                    v = round(float(v), 4)
                target[fld] = v


def _invert_top_elastic(layout, edited):
    """Fold edited BASE elements back into the layout's nested schema:
    topbar.<name> → cx/offsetTop/w; deploy.slot.<i> → cx[i-1] + shared offsetTop/w;
    deploy.<part> → dx/dy relative to the (edited) slot-1 center. cx/w round to 6;
    offsets keep 6 places (finer than baseline's 4 — this page is dy-sensitive).

    Flat pages (shop/henhouse/challenge) reuse mode "top-elastic" but have NO
    nested topbar/deploy dicts — fall back to the flat inverter for them."""
    if not isinstance(layout.get("topbar"), dict) and not isinstance(layout.get("deploy"), dict):
        _invert_flat_elements(layout, edited)
        return
    topbar = layout.get("topbar")
    if isinstance(topbar, dict):
        for name in _TOP_ELASTIC_TOPBAR:
            el = edited.get("topbar" + _ROW_SEP + name)
            tgt = topbar.get(name)
            if el is None or not isinstance(tgt, dict):
                continue
            if "cx" in el:
                tgt["cx"] = round(float(el["cx"]), 6)
            if isinstance(el.get("cy"), (int, float)):
                tgt["offsetTop"] = round(float(el["cy"]), 6)
            if "w" in el:
                tgt["w"] = round(float(el["w"]), 6)
    dep = layout.get("deploy")
    if not isinstance(dep, dict):
        return
    cx_arr = dep.get("cx")
    if isinstance(cx_arr, list) and cx_arr:
        slot_cys, slot_ws = [], []
        for i in range(1, len(cx_arr) + 1):
            el = edited.get("deploy" + _ROW_SEP + "slot" + _ROW_SEP + str(i))
            if el is None:
                continue
            if "cx" in el:
                cx_arr[i - 1] = round(float(el["cx"]), 6)
            if isinstance(el.get("cy"), (int, float)):
                slot_cys.append(float(el["cy"]))
            if "w" in el:
                slot_ws.append(float(el["w"]))
        # slots share one offsetTop / w — take the mean of what was dragged.
        if slot_cys:
            dep["offsetTop"] = round(sum(slot_cys) / len(slot_cys), 6)
        if slot_ws:
            dep["w"] = round(sum(slot_ws) / len(slot_ws), 6)
    # anchor for inner parts = the (now-updated) slot-1 center.
    anchor_cx = _num(cx_arr[0], 0.5) if isinstance(cx_arr, list) and cx_arr else 0.5
    anchor_cy = _num(dep.get("offsetTop"), 0.0)
    for part, has_w in _TOP_ELASTIC_DEPLOY_PARTS:
        el = edited.get("deploy" + _ROW_SEP + part)
        tgt = dep.get(part)
        if el is None or not isinstance(tgt, dict):
            continue
        if "cx" in el:
            tgt["dx"] = round(float(el["cx"]) - anchor_cx, 6)
        if isinstance(el.get("cy"), (int, float)):
            tgt["dy"] = round(float(el["cy"]) - anchor_cy, 6)
        if has_w and "w" in el:
            tgt["w"] = round(float(el["w"]), 6)


# --- overlay projection (mode == "overlay") ---------------------------------
# The loadout PANEL pack (resolveLoadoutPanelLayout). This pack's OWN canvas
# origin IS the overlay top (projected with overlayTop=0; at runtime the game
# re-bases it under the deploy row). Grid centers come from colCx[] × rows
# generated by firstCy/rowPitch; one vertical unit is frac×designH×widthScale
# (widthScale = canvas_w/720), normalized by canvas_h → k = designH·widthScale/
# canvas_h. Card inner parts + mystery are dx/dy offsets from a card center (dx a
# fraction of W, dy scaled by the same k). We project a small grid of card-center
# MARKERS (tune colCx/firstCy/rowPitch) + ONE reference card's inner parts + the
# mystery parts (tune the shared dx/dy/w).
# NOTE: "frame" is intentionally NOT here — the grid markers (grid.r0c0…) already
# render the card frame art per cell. Re-adding it would double-draw the wood
# frame on r0c0 (a real overlap bug). The card's INNER parts anchor to r0c0.
_OVERLAY_CARD_PARTS = ("portrait", "fpbox", "fplabel", "hex", "hexnum",
                       "ribbon", "btn", "arrow")
_OVERLAY_MYSTERY_PARTS = ("box", "label")
_OVERLAY_GRID_ROWS = 2  # sample rows projected as draggable card-center markers


def _overlay_k(layout, canvas):
    """The vertical-unit factor: a design-height fraction × k == a canvas-height
    fraction. k = designH · (canvas_w/720) / canvas_h."""
    overlay = layout.get("overlay") if isinstance(layout.get("overlay"), dict) else {}
    ch = _num(canvas.get("h"), 1600)
    cw = _num(canvas.get("w"), 720)
    design_h = _num(overlay.get("designH"), ch)
    if ch == 0:
        return 1.0
    return design_h * (cw / 720.0) / ch


def _project_overlay(layout, canvas, profiles):
    """Project the loadout PANEL pack → draggable editor elements.

    grid.r<row>c<col>   card-center markers (2×ncols): cx=colCx[col], cy=(firstCy+row·rowPitch)·k
    card.<part>         cx=colCx[0]+dx,  cy=row0Center+dy·k,  w=part.w
    mystery.<part>      cx=colCx[0]+dx,  cy=row1Center+dy·k,  w=part.w

    Mirrors resolveLoadoutPanelLayout with overlayTop=0, normalized by canvas."""
    out = []
    grid = layout.get("grid") if isinstance(layout.get("grid"), dict) else {}
    col_cx = grid.get("colCx")
    if not isinstance(col_cx, list) or not col_cx:
        col_cx = [0.5]
    first_cy = _num(grid.get("firstCy"), 0.0)
    row_pitch = _num(grid.get("rowPitch"), 0.0)
    k = _overlay_k(layout, canvas)

    def center_cy(row):
        return (first_cy + row * row_pitch) * k

    ncols = len(col_cx)
    card = layout.get("card") if isinstance(layout.get("card"), dict) else {}
    frame = card.get("frame") if isinstance(card.get("frame"), dict) else {}
    frame_w = frame.get("w")
    frame_tex = frame.get("tex")  # each grid cell shows the card frame art
    for row in range(_OVERLAY_GRID_ROWS):
        for col in range(ncols):
            el = {"id": "grid" + _ROW_SEP + ("r%dc%d" % (row, col)),
                  "cx": _num(col_cx[col], 0.5), "cy": center_cy(row)}
            if isinstance(frame_w, (int, float)):
                el["w"] = frame_w
            _resolve_file(el, frame_tex, profiles)
            out.append(el)
    # reference card inner parts anchored to grid r0c0 center.
    ref0_cx = _num(col_cx[0], 0.5)
    ref0_cy = center_cy(0)
    for part in _OVERLAY_CARD_PARTS:
        pv = card.get(part)
        if not isinstance(pv, dict):
            continue
        el = {"id": "card" + _ROW_SEP + part,
              "cx": ref0_cx + _num(pv.get("dx"), 0.0),
              "cy": ref0_cy + _num(pv.get("dy"), 0.0) * k}
        if isinstance(pv.get("w"), (int, float)):
            el["w"] = pv["w"]
        # calibTex = a preview-only portrait (大黄) for size calibration; the game
        # never reads it (portraits are per-tower at runtime). tex wins if present.
        _resolve_file(el, pv.get("tex") or pv.get("calibTex"), profiles)
        out.append(el)
    # mystery parts anchored to grid r0c1 — the SAME row as the reference card
    # (r0c0) but the next column over — so the owner can compare the owned-tower
    # card and the locked (mystery) card side by side at one baseline and drag the
    # heights into agreement. A different CELL → no overlap with the reference
    # card. Game meaning is preserved: dx/dy are an offset from a cell center, and
    # inversion (below) folds back against this SAME col-1/row-0 reference.
    mys = layout.get("mystery") if isinstance(layout.get("mystery"), dict) else {}
    mys_col = 1 if ncols > 1 else 0
    mys_cx = _num(col_cx[mys_col], 0.5)
    ref1_cy = center_cy(0)
    for part in _OVERLAY_MYSTERY_PARTS:
        pv = mys.get(part)
        if not isinstance(pv, dict):
            continue
        el = {"id": "mystery" + _ROW_SEP + part,
              "cx": mys_cx + _num(pv.get("dx"), 0.0),
              "cy": ref1_cy + _num(pv.get("dy"), 0.0) * k}
        if isinstance(pv.get("w"), (int, float)):
            el["w"] = pv["w"]
        _resolve_file(el, pv.get("tex"), profiles)
        out.append(el)
    return out


def _invert_overlay(layout, canvas, edited):
    """Fold edited PANEL elements back into the nested schema:
    grid markers → colCx[] (per-column mean) + firstCy (row-0 mean) + rowPitch
    (row1−row0, both divided back through k); card.<part> → dx/dy/w relative to
    the (updated) row-0 center; mystery.<part> → dx (vs col 0) / dy (vs row-1)/w."""
    k = _overlay_k(layout, canvas)
    inv_k = (1.0 / k) if k else 1.0
    grid = layout.get("grid")
    col_cx = grid.get("colCx") if isinstance(grid, dict) else None
    ncols = len(col_cx) if isinstance(col_cx, list) and col_cx else 1

    markers = {}
    for row in range(_OVERLAY_GRID_ROWS):
        for col in range(ncols):
            el = edited.get("grid" + _ROW_SEP + ("r%dc%d" % (row, col)))
            if el is not None:
                markers[(row, col)] = el

    if isinstance(grid, dict):
        if isinstance(col_cx, list):
            for col in range(ncols):
                xs = [float(markers[(r, col)]["cx"]) for r in range(_OVERLAY_GRID_ROWS)
                      if (r, col) in markers and "cx" in markers[(r, col)]]
                if xs:
                    col_cx[col] = round(sum(xs) / len(xs), 6)
        row0 = [float(markers[(0, c)]["cy"]) for c in range(ncols)
                if (0, c) in markers and isinstance(markers[(0, c)].get("cy"), (int, float))]
        row1 = [float(markers[(1, c)]["cy"]) for c in range(ncols)
                if (1, c) in markers and isinstance(markers[(1, c)].get("cy"), (int, float))]
        if row0:
            grid["firstCy"] = round((sum(row0) / len(row0)) * inv_k, 6)
        if row0 and row1:
            grid["rowPitch"] = round((sum(row1) / len(row1) - sum(row0) / len(row0)) * inv_k, 6)

    # recompute reference centers from the (updated) grid for part inversion.
    first_cy = _num(grid.get("firstCy"), 0.0) if isinstance(grid, dict) else 0.0
    row_pitch = _num(grid.get("rowPitch"), 0.0) if isinstance(grid, dict) else 0.0
    ref0_cx = _num(col_cx[0], 0.5) if isinstance(col_cx, list) and col_cx else 0.5
    ref0_cy = first_cy * k
    # mystery is projected at r0c1 (same row as the card, next column). Fold its
    # edits back against that SAME reference: col-1 x, row-0 y.
    mys_col = 1 if ncols > 1 else 0
    mys_cx = _num(col_cx[mys_col], 0.5) if isinstance(col_cx, list) and col_cx else 0.5
    ref1_cy = first_cy * k

    card = layout.get("card")
    if isinstance(card, dict):
        for part in _OVERLAY_CARD_PARTS:
            el = edited.get("card" + _ROW_SEP + part)
            tgt = card.get(part)
            if el is None or not isinstance(tgt, dict):
                continue
            if "cx" in el:
                tgt["dx"] = round(float(el["cx"]) - ref0_cx, 6)
            if isinstance(el.get("cy"), (int, float)):
                tgt["dy"] = round((float(el["cy"]) - ref0_cy) * inv_k, 6)
            if "w" in el:
                tgt["w"] = round(float(el["w"]), 6)
    mys = layout.get("mystery")
    if isinstance(mys, dict):
        for part in _OVERLAY_MYSTERY_PARTS:
            el = edited.get("mystery" + _ROW_SEP + part)
            tgt = mys.get(part)
            if el is None or not isinstance(tgt, dict):
                continue
            if "cx" in el:
                tgt["dx"] = round(float(el["cx"]) - mys_cx, 6)
            if isinstance(el.get("cy"), (int, float)):
                tgt["dy"] = round((float(el["cy"]) - ref1_cy) * inv_k, 6)
            if "w" in el:
                tgt["w"] = round(float(el["w"]), 6)


# --- dialog projection (mode == "dialog") -----------------------------------
# The dialog base (ui/layout/base/dialog-layout.ts) is a TWO-LAYER model:
#   layer 1  `dialog`  = the panel FRAME's occupancy of the phone screen,
#                        fractions {cx,cy,w,h} of the canvas — dragged onto the
#                        screen base, then FIXED (铁律 1: never re-flow/stretch).
#   layer 2  elements  = each element's {cx,cy,w,h} is a fraction of the FRAME,
#                        not the canvas (铁律 2).
# EyeToSpec draws everything in ONE canvas-normalized space, so we project the
# frame-relative elements down to canvas fractions exactly like the game's
# resolveDialogElement (box.left + cx·box.w), and surface the frame ITSELF as a
# draggable element so the owner drags the panel onto the screen first, then the
# content inside it. Save inverts both (see _invert_dialog). Because EyeToSpec's
# canvas.h == the game's fixed DIALOG_DESIGN_HEIGHT (1560), a canvas-fraction of
# the frame height equals the game's fixed-height projection — no live re-flow.
def _project_dialog(layout, profiles):
    frame = layout.get("dialog") if isinstance(layout.get("dialog"), dict) else {}
    fcx = _num(frame.get("cx"), 0.5)
    fcy = _num(frame.get("cy"), 0.5)
    fw = _num(frame.get("w"), 0.8)
    fh = _num(frame.get("h"), 0.6)
    left = fcx - fw / 2.0
    top = fcy - fh / 2.0

    out = []
    # The frame itself, draggable on the screen base (layer 1). Its wood-frame art
    # (tex) contain-fits the frame box, matching the runtime panel.
    frame_el = {"id": "dialog", "cx": fcx, "cy": fcy, "w": fw, "h": fh}
    _resolve_file(frame_el, frame.get("tex"), profiles)
    out.append(frame_el)

    # Each content element: frame-relative fraction → canvas fraction (layer 2).
    for key, v in layout.items():
        if key in _LAYOUT_META_KEYS or key == "dialog" or not isinstance(v, dict):
            continue
        if "cx" not in v or "cy" not in v:
            continue
        ew = _num(v.get("w"), 0.1)
        eh = _num(v.get("h"), ew)
        el = {"id": key,
              "cx": left + _num(v.get("cx"), 0.5) * fw,
              "cy": top + _num(v.get("cy"), 0.5) * fh,
              "w": ew * fw,
              "h": eh * fh}
        # Text / paint style fields pass through verbatim (already canvas-neutral).
        for fld in ("anchor", "rotation", "text", "color", "align", "fontSize",
                    "fontFamily", "fontWeight", "stroke", "strokeWidth", "shadow",
                    "fill", "alpha", "radius"):
            if fld in v:
                el[fld] = v[fld]
        _resolve_file(el, v.get("tex"), profiles)
        out.append(el)
    return out


def _invert_dialog(layout, edited):
    """Fold edited canvas-space elements back into the two-layer dialog schema:
    the `dialog` element restores the frame {cx,cy,w,h}; every other element's
    canvas fraction is divided back through the (updated) frame to recover its
    FRAME-relative {cx,cy,w,h} — the exact inverse of _project_dialog."""
    # 0. Hard-delete: any frame-relative element key present in the layout but
    # absent from the edited manifest was soft-deleted in the editor — drop it for
    # good (mirrors _invert_flat_elements). The `dialog` frame itself is never a
    # content element, so it's excluded and can't be deleted this way.
    existing = [k for k, v in layout.items()
                if k not in _LAYOUT_META_KEYS and k != "dialog" and isinstance(v, dict)
                and "cx" in v and "cy" in v]
    for k in existing:
        if k not in edited:
            del layout[k]

    # 1. Restore the frame first (later elements invert against the NEW frame).
    frame = layout.get("dialog") if isinstance(layout.get("dialog"), dict) else None
    fe = edited.get("dialog")
    if isinstance(frame, dict) and isinstance(fe, dict):
        for fld in ("cx", "cy", "w", "h"):
            if isinstance(fe.get(fld), (int, float)):
                frame[fld] = round(float(fe[fld]), 6)
    fcx = _num(frame.get("cx"), 0.5) if isinstance(frame, dict) else 0.5
    fcy = _num(frame.get("cy"), 0.5) if isinstance(frame, dict) else 0.5
    fw = _num(frame.get("w"), 0.8) if isinstance(frame, dict) else 0.8
    fh = _num(frame.get("h"), 0.6) if isinstance(frame, dict) else 0.6
    left = fcx - fw / 2.0
    top = fcy - fh / 2.0

    # 2. Each content element: canvas fraction → frame-relative fraction.
    for key, el in edited.items():
        if key == "dialog" or key in _LAYOUT_META_KEYS:
            continue
        target = layout.get(key)
        if not isinstance(target, dict):
            continue
        if isinstance(el.get("cx"), (int, float)) and fw:
            target["cx"] = round((float(el["cx"]) - left) / fw, 6)
        if isinstance(el.get("cy"), (int, float)) and fh:
            target["cy"] = round((float(el["cy"]) - top) / fh, 6)
        if isinstance(el.get("w"), (int, float)) and fw:
            target["w"] = round(float(el["w"]) / fw, 6)
        if isinstance(el.get("h"), (int, float)) and fh:
            target["h"] = round(float(el["h"]) / fh, 6)
        # Style fields written straight back (canvas-neutral).
        for fld in ("text", "color", "align", "fontSize", "fontFamily",
                    "fontWeight", "stroke", "strokeWidth", "shadow", "fill",
                    "alpha", "radius"):
            if fld in el:
                target[fld] = el[fld]


# --- combine view (source with a `combine` array) ---------------------------
# A combine source declares NO coordinates of its own: it references two child
# layouts (a top-elastic BASE + an overlay PANEL) and serve.py stacks them into
# ONE full-page canvas at the game's true runtime relationship —
#   overlayTop = base.deployRowBottom + panel.overlay.gapTop·W
#   (deployRowBottom = base.deploy.offsetTop·H + base.deploy.w·W/2)
# — the SAME math as resolveLoadoutBaseLayout/PanelLayout. BASE elements keep
# their full-page position; PANEL elements shift DOWN by overlayTop. Every id is
# namespaced ("base::…" / "panel::…") so a drag routes back to the right child
# json (base → _invert_top_elastic, panel → un-shift then _invert_overlay). The
# combine view never writes coordinates itself — it only edits through the
# children, so base/panel/combine never drift.
_COMBINE_SEP = "::"


def _combine_children(source):
    """Resolve a combine source's child specs → [(role, child_source_dict), …].
    Each child_source reuses the parent repo unless it carries its own."""
    repo = source.get("repo", "~")
    out = []
    for spec in source.get("combine", []):
        if not isinstance(spec, dict) or "layout" not in spec:
            continue
        out.append((spec.get("role", ""),
                    {"repo": spec.get("repo", repo), "layout": spec["layout"]}))
    return out


def _combine_overlay_top(base_layout, panel_layout, canvas):
    """The overlay origin in canvas px, mirroring the game exactly:
    deployRowBottom = deploy.offsetTop·H + deploy.w·W/2;
    overlayTop = deployRowBottom + overlay.gapTop·W."""
    w = _num(canvas.get("w"), 720)
    h = _num(canvas.get("h"), 1600)
    dep = base_layout.get("deploy") if isinstance(base_layout.get("deploy"), dict) else {}
    deploy_row_bottom = _num(dep.get("offsetTop"), 0.0) * h + _num(dep.get("w"), 0.0) * w / 2.0
    overlay = panel_layout.get("overlay") if isinstance(panel_layout.get("overlay"), dict) else {}
    return deploy_row_bottom + _num(overlay.get("gapTop"), 0.0) * w


def _head_height_px(base_layout, canvas, resource_root, profiles):
    """The fixed head's rendered bottom edge in canvas px, mirroring the scene:
    headHeight = width × (bgTop natural height / natural width). The scene uses the
    BG_TOP_SRC constant ratio; EyeToSpec draws the actual art, so we read the real
    bg-top image aspect (≡ what's on screen). Falls back to the game's 904/1345."""
    w = _num(canvas.get("w"), 720)
    ratio = 904.0 / 1345.0
    bg = base_layout.get("bg") if isinstance(base_layout.get("bg"), dict) else {}
    tex = bg.get("top")
    if tex in profiles:
        scene, fmt = profiles[tex]
        path = os.path.join(resource_root, scene, "%s.%s" % (tex, fmt))
        try:
            from PIL import Image
            with Image.open(path) as im:
                if im.width:
                    ratio = im.height / im.width
        except Exception:
            pass
    return w * ratio


def _panel_screen_shift(source, panel_layout, canvas, resource_root, profiles):
    """For a PANEL pack that declares `baseLayout`, return the screen-placement
    geometry the scene composes at runtime, all as canvas-height fractions:
      shift    = overlayTop/H     (candidate origin; every card cy shifts down by it)
      head_cy  = headHeight/H     (fixed head's bottom edge — the read-only line)
      bg_top_cy= (headHeight-tuck)/H  (scroll-bg top; tucks just above the line)
    Returns None when no baseLayout is declared (pack stays overlay-local)."""
    base_rel = source.get("baseLayout")
    if not base_rel:
        return None
    repo = _expand(source.get("repo", "~"))
    try:
        base_layout = json.load(open(os.path.join(repo, base_rel), encoding="utf-8"))
    except Exception:
        return None
    h = _num(canvas.get("h"), 1600)
    w = _num(canvas.get("w"), 720)
    if not h:
        return None
    overlay_top = _combine_overlay_top(base_layout, panel_layout, canvas)
    head_px = _head_height_px(base_layout, canvas, resource_root, profiles)
    tuck = max(2.0, round(w * 0.015))  # scene: Math.max(2, round(width*0.015))
    return {"shift": overlay_top / h, "head_cy": head_px / h,
            "bg_top_cy": (head_px - tuck) / h}


def build_combine_manifest(source):
    """Build a stacked full-page manifest from a combine source. Returns
    (manifest, resource_root). Child element ids are prefixed role::id; PANEL
    ys are shifted down by overlayTop/H so the whole page reads as it renders."""
    children = _combine_children(source)
    base_c = next((c for r, c in children if r == "base"), None)
    panel_c = next((c for r, c in children if r == "panel"), None)
    if base_c is None or panel_c is None:
        raise FileNotFoundError("combine source needs both a base and a panel child")

    base_man, resource_root = build_source_manifest(base_c)
    panel_man, _ = build_source_manifest(panel_c)

    # Full-page canvas: use the base child's canvas (topbar+deploy live there and
    # it's already the full 720×1600 page). PANEL child shares the same H.
    canvas = base_man.get("canvas", {"w": 720, "h": 1600})
    h = _num(canvas.get("h"), 1600)

    # Recompute overlayTop from the RAW child layouts (not the manifests) so the
    # deploy/overlay math is authoritative.
    base_layout = json.load(open(os.path.join(_expand(base_c["repo"]), base_c["layout"]), encoding="utf-8"))
    panel_layout = json.load(open(os.path.join(_expand(panel_c["repo"]), panel_c["layout"]), encoding="utf-8"))
    overlay_top = _combine_overlay_top(base_layout, panel_layout, canvas)
    shift = (overlay_top / h) if h else 0.0

    elements = []
    for el in base_man.get("elements", []):
        e = dict(el)
        e["id"] = "base" + _COMBINE_SEP + el["id"]
        elements.append(e)
    for el in panel_man.get("elements", []):
        e = dict(el)
        e["id"] = "panel" + _COMBINE_SEP + el["id"]
        if isinstance(e.get("cy"), (int, float)):
            e["cy"] = round(float(e["cy"]) + shift, 6)  # push overlay down under the deploy row
        elements.append(e)

    # Two-layer background stacked top→bottom: the hut head (base bg) then the
    # grass body (panel bg), the front-end seats each by natural aspect so grass
    # sits exactly at the head's rendered cut line (the true runtime seam) and
    # tiles down (repeat) to fill the rest. Minimal + faithful: no hardcoded art
    # ratios in serve.py, robust to art-size changes.
    # Two physical layers, mirroring the game: the FIXED hut head (base bg-top,
    # never scrolls) once, then the SCROLL body — a single pre-stitched long image
    # (panel loadout-scroll-bg = bg-bottom + grass baked offline). Neither repeats;
    # the scroll image already covers the candidate region. Its top tucks under the
    # head (see guideLine / the game's tuck), so it's seated at the head's cut line.
    backgrounds = []
    if isinstance(base_man.get("background"), dict):
        backgrounds.append({"file": base_man["background"]["file"], "cover": False, "repeat": False})
    if isinstance(panel_man.get("background"), dict):
        backgrounds.append({"file": panel_man["background"]["file"], "cover": False, "repeat": False})

    manifest = {
        "name": source.get("name") or "",
        "description": source.get("_comment", ""),
        "canvas": canvas,
        "elements": elements,
        "combine": True,
        # The overlay-top guide line: a READ-ONLY horizontal marker at the exact
        # y the game computes for the candidate overlay origin (deployRowBottom +
        # gapTop·W). Panel elements are already shifted down by this same value,
        # so the line reads as "candidates lay out below here". Derived, never
        # dragged — it moves only when the base deploy row / gapTop change.
        "guideLine": round(shift, 6),
    }
    if backgrounds:
        manifest["backgrounds"] = backgrounds
    if isinstance(base_man.get("safe"), dict):
        manifest["safe"] = base_man["safe"]
    if base_man.get("showCapsule"):
        manifest["showCapsule"] = True
    return manifest, resource_root


def write_combine_manifest(source, manifest):
    """Route an edited combine manifest back into its two child layouts.
    base:: elements → _invert_top_elastic(base json); panel:: elements → subtract
    overlayTop (recomputed from the PRE-edit base layout, same value used to build)
    then _invert_overlay(panel json). Returns (path_summary, added=0)."""
    children = _combine_children(source)
    base_c = next((c for r, c in children if r == "base"), None)
    panel_c = next((c for r, c in children if r == "panel"), None)
    if base_c is None or panel_c is None:
        raise FileNotFoundError("combine source needs both a base and a panel child")

    base_path = os.path.join(_expand(base_c["repo"]), base_c["layout"])
    panel_path = os.path.join(_expand(panel_c["repo"]), panel_c["layout"])
    base_layout = json.load(open(base_path, encoding="utf-8"))
    panel_layout = json.load(open(panel_path, encoding="utf-8"))

    meta = base_layout.get("_eyetospec", {}) if isinstance(base_layout, dict) else {}
    canvas = meta.get("canvas", {"w": 720, "h": 1600})
    h = _num(canvas.get("h"), 1600)
    # overlayTop from the PRE-edit layouts == the value the manifest was built with,
    # so the panel un-shift exactly cancels the build-time shift (zero drift).
    overlay_top = _combine_overlay_top(base_layout, panel_layout, canvas)
    shift = (overlay_top / h) if h else 0.0

    edited = {el["id"]: el for el in manifest.get("elements", []) if isinstance(el, dict) and "id" in el}
    base_edited, panel_edited = {}, {}
    for eid, el in edited.items():
        if _COMBINE_SEP not in eid:
            continue
        role, child_id = eid.split(_COMBINE_SEP, 1)
        child_el = dict(el)
        child_el["id"] = child_id
        if role == "base":
            base_edited[child_id] = child_el
        elif role == "panel":
            if isinstance(child_el.get("cy"), (int, float)):
                child_el["cy"] = float(child_el["cy"]) - shift  # back to overlay-local
            panel_edited[child_id] = child_el

    _invert_top_elastic(base_layout, base_edited)
    _invert_overlay(panel_layout, canvas, panel_edited)

    with open(base_path, "w", encoding="utf-8") as f:
        json.dump(base_layout, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(panel_path, "w", encoding="utf-8") as f:
        json.dump(panel_layout, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return "%s + %s" % (base_path, panel_path), 0


def _expand(path):
    return os.path.abspath(os.path.expanduser(path))


def read_source(pack_dir):
    """Read an optional source.json (live game-config pointer). {} if absent."""
    path = os.path.join(pack_dir, "source.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_asset_profiles(profiles_path):
    """key -> (scene subdir, format) from the repo's asset-profiles.json."""
    out = {}
    try:
        with open(profiles_path, "r", encoding="utf-8") as f:
            for p in json.load(f):
                out[p.get("key")] = (p.get("scene"), p.get("format", "png"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return out


def build_source_manifest(source):
    """Turn a source.json into a live pack manifest read from the game config.

    Returns (manifest_dict, resource_root_abspath). Raises FileNotFoundError if
    the layout file is missing."""
    # Combine source: no layout of its own — stack its child packs instead.
    if isinstance(source.get("combine"), list) and source["combine"]:
        return build_combine_manifest(source)
    repo = _expand(source.get("repo", "~"))
    layout_path = os.path.join(repo, source.get("layout", ""))
    with open(layout_path, "r", encoding="utf-8") as f:
        layout = json.load(f)

    meta = layout.get("_eyetospec", {}) if isinstance(layout, dict) else {}
    canvas = meta.get("canvas", {"w": 720, "h": 1280})
    resource_root = _expand(os.path.join(repo, meta.get("resourceRoot", "")))
    profiles = _load_asset_profiles(_expand(os.path.join(repo, meta.get("assetProfiles", "")))) \
        if meta.get("assetProfiles") else {}

    mode = layout.get("mode")
    is_baseline = mode == "baseline-layout"
    baseline_ratio = _num(layout.get("baselineRatio"), 0.5)

    # Loadout two-pack (nested / generative schemas): the topbar+deploy BASE pack
    # (top-elastic) and the grid+card+mystery PANEL pack (overlay) don't carry
    # literal top-level cx/cy, so the flat loop below can't see them. Project them
    # into draggable elements with dedicated helpers whose math mirrors the game's
    # resolveLoadout*Layout exactly.
    # Dialog pages (mode "dialog", OR any layout carrying a top-level `dialog`
    # frame): a two-layer model — the frame is a draggable element and the content
    # elements are frame-relative, projected to canvas space by _project_dialog.
    if mode == "dialog" or isinstance(layout.get("dialog"), dict):
        elements = _project_dialog(layout, profiles)
        manifest = {
            "name": source.get("name") or meta.get("name") or "",
            "description": source.get("_comment", ""),
            "canvas": canvas,
            "elements": elements,
        }
        safe = meta.get("safeArea")
        if isinstance(safe, dict):
            manifest["safe"] = {"top": safe.get("top", 0), "bottom": safe.get("bottom", 0)}
        if meta.get("showCapsule"):
            manifest["showCapsule"] = True
        return manifest, resource_root

    if mode in ("top-elastic", "overlay"):
        if mode == "top-elastic":
            elements = _project_top_elastic(layout, profiles)
        else:
            elements = _project_overlay(layout, canvas, profiles)
        # Screen placement for an overlay PANEL that declares a baseLayout: mirror
        # the scene composing base+panel. Shift every candidate down by overlayTop
        # so it lands at its true screen y; the line sits at the head's bottom edge
        # and the scroll-bg tucks just above it (see _panel_screen_shift). Without a
        # baseLayout the pack stays overlay-local (origin at y=0, unchanged).
        shift = _panel_screen_shift(source, layout, canvas, resource_root, profiles) \
            if mode == "overlay" else None
        if shift:
            for el in elements:
                if isinstance(el.get("cy"), (int, float)):
                    el["cy"] = round(el["cy"] + shift["shift"], 6)
        manifest = {
            "name": source.get("name") or meta.get("name") or "",
            "description": source.get("_comment", ""),
            "canvas": canvas,
            "elements": elements,
        }
        if shift:
            # Read-only line at the fixed head's bottom edge — "浮层最底下的边缘".
            manifest["guideLine"] = round(shift["head_cy"], 6)
        safe = meta.get("safeArea")
        if isinstance(safe, dict):
            manifest["safe"] = {"top": safe.get("top", 0), "bottom": safe.get("bottom", 0)}
        bg = meta.get("background")
        if isinstance(bg, dict) and bg.get("tex") in profiles:
            scene, fmt = profiles[bg["tex"]]
            # The overlay bg is a single pre-stitched long image (loadout-scroll-bg):
            # WIDTH-filled, TOP-anchored, never repeats. When placed at screen
            # coords (baseLayout present), its top tucks under the head at
            # bg_top_cy = (headHeight-tuck)/H; otherwise it fills from y=0.
            # anchor picks the width-locked crop side: "top" (default) fills from
            # y=0 and crops off the bottom; "bottom"/"baseline" pins the art to the
            # screen bottom and crops off the TOP (the game's fillBackgroundWidth
            # anchor:"baseline" — e.g. endless, where the chick+platform sit at the
            # bottom and the tall bg overflows upward).
            anchor = str(bg.get("anchor", "top")).lower()
            fit = "width-bottom" if anchor in ("bottom", "baseline") else "width-top"
            layer = {"file": "%s/%s.%s" % (scene, bg["tex"], fmt),
                     "cover": bool(bg.get("cover")),
                     "fit": fit,
                     "repeat": False}
            if shift:
                layer["topCy"] = round(shift["bg_top_cy"], 6)
            manifest["background"] = layer
        if meta.get("showCapsule"):
            manifest["showCapsule"] = True
        return manifest, resource_root

    elements = []
    for key, v in layout.items():
        if key in _LAYOUT_META_KEYS or not isinstance(v, dict):
            continue
        if is_baseline:
            # Multi-point row (resourceBar/arrows/tabs): explode into one draggable
            # child per x field, then done with this key.
            if key in _BASELINE_ROWS:
                elements.extend(_project_baseline_row(key, v, baseline_ratio, profiles))
                continue
            # Equal-spaced array (chests): explode into N icons sharing cx/gap/size.
            if key in _BASELINE_ARRAYS:
                elements.extend(_project_baseline_array(key, v, baseline_ratio, profiles))
                continue
            # baseline single-point element: project its offset → a literal cy.
            cy = _project_baseline_element(key, v, baseline_ratio)
            if cy is None:
                continue  # no single-point cx: not editable here
            el = {"id": key, "cx": v.get("cx"), "cy": cy}
            for fld in ("w", "h", "rotation"):
                if fld in v:
                    el[fld] = v[fld]
        else:
            if "cx" not in v or "cy" not in v:
                continue
            el = {"id": key}
            for fld in _ELEM_PASS:
                if fld in v:
                    el[fld] = v[fld]
        tex = v.get("tex")
        if tex and tex in profiles:
            scene, fmt = profiles[tex]
            el["file"] = "%s/%s.%s" % (scene, tex, fmt)  # relative to resourceRoot
        elements.append(el)

    # Baseline pages: seed the draggable baseline at baselineRatio (== originY/H
    # in the matched canvas). Owner drags it to the bg nest; save writes back
    # baselineRatio + bg.anchorY together (they converge here).
    baseline_seed = baseline_ratio if is_baseline else None

    manifest = {
        "name": source.get("name") or meta.get("name") or "",
        "description": source.get("_comment", ""),
        "canvas": canvas,
        "elements": elements,
    }
    # Baseline pages expose a draggable baseline (app.js anchorLine); seed it at
    # baselineRatio so the owner drags ONE line to pin hen + mark the bg nest.
    if baseline_seed is not None:
        manifest["anchorLine"] = {"cx": 0.5, "cy": baseline_seed, "w": 1, "h": 0.04}
    # Review-only safe-area overlay (notch / home indicator), fractions of canvas
    # height. Declared in the config's _eyetospec block so it travels with the
    # single source; the editor draws it when no ?safe= query overrides.
    safe = meta.get("safeArea")
    if isinstance(safe, dict):
        manifest["safe"] = {"top": safe.get("top", 0), "bottom": safe.get("bottom", 0)}
    # Page background the game draws via fillBackgroundWidth (not a layout element):
    # named in _eyetospec.background so EyeToSpec shows it too. tex → file via profiles.
    bg = meta.get("background")
    if isinstance(bg, dict) and bg.get("tex") in profiles:
        scene, fmt = profiles[bg["tex"]]
        # anchor picks the width-locked crop side: "top" (default) fills from y=0
        # and crops the bottom; "bottom"/"baseline" pins art to the screen bottom
        # and crops the TOP (fillBackgroundWidth anchor:"baseline" — e.g. endless,
        # whose tall bg keeps the chick+platform at the bottom and overflows upward).
        anchor = str(bg.get("anchor", "top")).lower()
        fit = "width-bottom" if anchor in ("bottom", "baseline") else "width-top"
        manifest["background"] = {"file": "%s/%s.%s" % (scene, bg["tex"], fmt),
                                  "cover": bool(bg.get("cover")),
                                  "fit": fit,
                                  "repeat": False}
    # WeChat top-right menu-capsule forbidden zone (review guide).
    if meta.get("showCapsule"):
        manifest["showCapsule"] = True
    return manifest, resource_root


def _verify_writeback_schema(layout, edited):
    """Guard against the silent-drop class of bug: the edited manifest's element
    ids must fit the schema the layout will be inverted through, or the save would
    write nothing (or the wrong thing) while reporting success.

    The known trap: mode "top-elastic" is shared by the loadout (NESTED topbar/
    deploy dicts, ids like "topbar.back"/"deploy.slot.1") and the flat pages
    (shop/henhouse/challenge, ids are plain top-level keys). If an edited id set
    that clearly belongs to one shape arrives for a layout of the other shape,
    raise ValueError so the POST fails loudly instead of dropping edits."""
    if layout.get("mode") != "top-elastic":
        return  # baseline / overlay have their own dedicated, matched inverters
    is_nested = isinstance(layout.get("topbar"), dict) or isinstance(layout.get("deploy"), dict)
    ids = [k for k in edited if k not in _LAYOUT_META_KEYS]
    if not ids:
        return
    nested_ids = [k for k in ids if k.startswith("topbar" + _ROW_SEP) or k.startswith("deploy" + _ROW_SEP)]
    if is_nested and not nested_ids:
        raise ValueError(
            "schema mismatch: layout uses the NESTED top-elastic schema "
            "(topbar/deploy dicts) but no edited element ids match it — refusing "
            "to save (edits would be silently dropped)")
    if not is_nested:
        # Flat layout: every edited id (that isn't a duplicate the editor created)
        # should be an existing top-level placeable key. A stray nested id means
        # the manifest was built for the wrong schema.
        if nested_ids:
            raise ValueError(
                "schema mismatch: layout uses the FLAT top-elastic schema (plain "
                "top-level keys) but got nested ids %s — refusing to save" % nested_ids[:3])


def write_source_manifest(source, manifest):
    """Write an edited manifest BACK into the source-bound game layout file.

    The inverse of build_source_manifest: for each edited element we update ONLY
    the placement fields (_ELEM_PASS) on the matching layout key, preserving the
    game's own schema — tex, _eyetospec, page metadata, and any keys the editor
    never surfaced. This keeps source.json a true live link (edit in the editor →
    the hand-authored config updates in place), so NO pack.json snapshot is made
    and the game + EyeToSpec never drift. Returns the layout path written."""
    # Combine source: route edited elements back into the two child layouts.
    if isinstance(source.get("combine"), list) and source["combine"]:
        return write_combine_manifest(source, manifest)
    repo = _expand(source.get("repo", "~"))
    layout_path = os.path.join(repo, source.get("layout", ""))
    with open(layout_path, "r", encoding="utf-8") as f:
        layout = json.load(f)
    edited = {el["id"]: el for el in manifest.get("elements", []) if isinstance(el, dict) and "id" in el}
    added = 0

    # Preflight: the edited element ids must match the schema the layout actually
    # uses, so a save can never silently drop edits (the top-elastic schema
    # collision: nested topbar/deploy vs flat top-level keys). Raise → the POST
    # returns 500 and the editor toasts the error instead of faking success.
    _verify_writeback_schema(layout, edited)

    mode = layout.get("mode")
    # Dialog pages: invert the two-layer projection (frame + frame-relative
    # elements) back into the game schema, preserving tex / _eyetospec / metadata.
    if mode == "dialog" or isinstance(layout.get("dialog"), dict):
        _invert_dialog(layout, edited)
        with open(layout_path, "w", encoding="utf-8") as f:
            json.dump(layout, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return layout_path, added

    # Loadout two-pack: invert the dedicated projections straight back into the
    # nested schema, preserving tex / _eyetospec / metadata (never a flat write).
    if mode in ("top-elastic", "overlay"):
        if mode == "top-elastic":
            _invert_top_elastic(layout, edited)
        else:
            meta = layout.get("_eyetospec", {}) if isinstance(layout, dict) else {}
            canvas = meta.get("canvas", {"w": 720, "h": 1600})
            # If the pack rendered at screen coords (baseLayout present), un-shift
            # every edited cy by the SAME overlayTop before inverting, so panel.json
            # stays overlay-local (zero drift — exact inverse of the build shift).
            resource_root = _expand(os.path.join(_expand(source.get("repo", "~")),
                                                  meta.get("resourceRoot", "")))
            profiles = _load_asset_profiles(_expand(os.path.join(_expand(source.get("repo", "~")),
                                                                  meta.get("assetProfiles", "")))) \
                if meta.get("assetProfiles") else {}
            shift = _panel_screen_shift(source, layout, canvas, resource_root, profiles)
            if shift:
                for el in edited.values():
                    if isinstance(el.get("cy"), (int, float)):
                        el["cy"] = round(float(el["cy"]) - shift["shift"], 6)
            _invert_overlay(layout, canvas, edited)
        with open(layout_path, "w", encoding="utf-8") as f:
            json.dump(layout, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return layout_path, added

    is_baseline = layout.get("mode") == "baseline-layout"
    # Baseline pages: the dragged baseline (anchorLine.cy) is the new pin AND the
    # bg nest anchor — write it back to BOTH baselineRatio and bg.anchorY so the
    # game re-pins the hen and re-aligns the bg to the same line the owner marked.
    # Read it BEFORE inverting elements (element offsets are relative to it).
    if is_baseline:
        line = manifest.get("anchorLine")
        if isinstance(line, dict) and isinstance(line.get("cy"), (int, float)):
            ratio = float(line["cy"])
            layout["baselineRatio"] = round(ratio, 4)
            if isinstance(layout.get("bg"), dict):
                layout["bg"]["anchorY"] = round(ratio, 4)
        baseline_ratio = _num(layout.get("baselineRatio"), 0.5)

    # Equal-spaced arrays need ALL their children together to re-derive the shared
    # cx (center) + gap (spacing), so aggregate them first, then fold in one shot.
    if is_baseline:
        _invert_baseline_arrays(layout, edited)

    for key, el in edited.items():
        # Array child ("<key>.<i>"): handled in the aggregate pass above; skip.
        if is_baseline and _ROW_SEP in key and key.split(_ROW_SEP, 1)[0] in _BASELINE_ARRAYS:
            continue
        # Multi-point row child ("<row>.<field>"): write cx back to the row's x
        # field, and re-derive the row's shared offset from cy. Several children
        # share one offset; writing the same value repeatedly is harmless.
        if is_baseline and _ROW_SEP in key:
            row_key, field = key.split(_ROW_SEP, 1)
            spec = _BASELINE_ROWS.get(row_key)
            row = layout.get(row_key)
            if spec and isinstance(row, dict) and any(field == f for f, _ in spec["points"]):
                if "cx" in el:
                    row[field] = round(float(el["cx"]), 4)
                if isinstance(el.get("cy"), (int, float)):
                    cy = float(el["cy"])
                    off = cy if spec["group"] == "top" else 1.0 - cy
                    row[spec["offset"]] = round(off, 4)
            continue

        target = layout.get(key)
        if not isinstance(target, dict):
            # A key the source layout doesn't have yet: an element the editor
            # created (Duplicate → "<id>-copy"). Materialize it as a real layout
            # entry so it survives the round-trip. The manifest carries `file`
            # (scene/tex.fmt) not `tex`, so recover the game's `tex` from the
            # filename stem — the exact inverse of build_source_manifest.
            if key in _LAYOUT_META_KEYS:
                continue  # never overwrite page metadata via a stray element id
            target = {}
            file = el.get("file")
            if isinstance(file, str) and file:
                target["tex"] = os.path.splitext(os.path.basename(file))[0]
            layout[key] = target
            added += 1
        if is_baseline:
            # Invert the cy projection back into the element's offset field; cx/w
            # pass through. (Elements not in a baseline group were never emitted,
            # so they can't appear here.)
            if "cx" in el:
                target["cx"] = round(float(el["cx"]), 4)
            if "w" in el:
                target["w"] = round(float(el["w"]), 4)
            if "h" in el:
                target["h"] = round(float(el["h"]), 4)
            if isinstance(el.get("cy"), (int, float)):
                inv = _invert_baseline_element(key, float(el["cy"]), baseline_ratio)
                if inv is not None:
                    target[inv[0]] = round(inv[1], 4)
            continue
        for fld in _ELEM_PASS:
            if fld in el:
                target[fld] = el[fld]
    with open(layout_path, "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return layout_path, added


def list_packs():
    """Scan ./config for packs, at most one level deep.

    A top-level dir with a pack.json is a standalone pack (id = "<name>").
    A top-level dir WITHOUT pack.json is a *group*: its child dirs that have a
    pack.json become frames (id = "<group>/<frame>"), returned as a group with an
    ordered `frames` list. A group.json may set the group name/description and an
    explicit frame `order`."""
    packs = []
    groups = []
    if not os.path.isdir(CONFIG_DIR):
        return {"packs": packs, "groups": groups}
    for name in sorted(os.listdir(CONFIG_DIR)):
        pack_dir = os.path.join(CONFIG_DIR, name)
        if not os.path.isdir(pack_dir):
            continue
        # a dir with pack.json OR source.json is a standalone pack
        if os.path.isfile(os.path.join(pack_dir, "pack.json")) or \
                os.path.isfile(os.path.join(pack_dir, "source.json")):
            entry = _read_pack_entry(name, pack_dir)
            if entry:
                packs.append(entry)
            continue
        # otherwise -> treat as a group of frame subdirs
        meta = read_group(pack_dir)
        frames = []
        for child in sorted(os.listdir(pack_dir)):
            child_dir = os.path.join(pack_dir, child)
            if not os.path.isdir(child_dir):
                continue
            if os.path.isfile(os.path.join(child_dir, "pack.json")) or \
                    os.path.isfile(os.path.join(child_dir, "source.json")):
                entry = _read_pack_entry(name + "/" + child, child_dir)
                if entry:
                    entry["frame"] = child
                    frames.append(entry)
        if not frames:
            continue
        order = meta.get("order")
        if isinstance(order, list):
            rank = {f: i for i, f in enumerate(order)}
            frames.sort(key=lambda e: (rank.get(e["frame"], len(order)), e["frame"]))
        groups.append({
            "id": name,
            "name": meta.get("name", name),
            "description": meta.get("description", ""),
            "frames": frames,
        })
    return {"packs": packs, "groups": groups}


def read_manifest(pack_id):
    pack_dir = os.path.join(CONFIG_DIR, *pack_id.split("/"))
    manifest = os.path.join(pack_dir, "pack.json")
    if os.path.isfile(manifest):
        with open(manifest, "r", encoding="utf-8") as f:
            return json.load(f)
    # live source-backed pack: build the manifest from the game config now.
    source = read_source(pack_dir)
    if source:
        data, _ = build_source_manifest(source)
        data["live"] = True
        return data
    raise FileNotFoundError(manifest)


def is_safe_id(pack_id):
    """A pack id is one dir name, or a group/frame pair ("group/frame").

    Allows at most one "/" so grouped frame sequences work; still rejects
    backslashes, "." / ".." segments, and empties."""
    if not pack_id or "\\" in pack_id:
        return False
    parts = pack_id.split("/")
    if len(parts) > 2:
        return False
    return all(p and p not in (".", "..") for p in parts)


def find_chrome():
    """Locate a headless-capable Chrome/Chromium binary for the screenshot API."""
    for name in ("google-chrome", "chromium-browser", "chromium",
                 "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.isfile(mac):
        return mac
    return None


def render_screenshot(port, pack_id, width, height, safe=None, capsule=None, baseline=None, line=None, min_h=None):
    """Headless-render editor.html?pack=<id>&render=1 and return PNG bytes.

    Agent-only preview: the render-mode page hides all editor chrome, so this
    captures a clean canvas. Waits on window.__ready via --virtual-time-budget.

    `safe` (e.g. "top:0.07,bottom:0.04") is forwarded to the render page to draw
    the safe-area overlay + flag any element crossing a safe line."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("no Chrome/Chromium found for --headless screenshot")
    url = "http://127.0.0.1:%d/editor.html?pack=%s&render=1" % (port, quote(pack_id, safe=""))
    if safe:
        url += "&safe=" + quote(safe, safe=":,")
    if capsule:
        url += "&capsule=1"
    if baseline:
        url += "&baseline=" + quote(str(baseline))
    if line:
        url += "&line=" + quote(str(line))
    if min_h:
        url += "&minH=" + quote(str(min_h))
    tmpdir = tempfile.mkdtemp(prefix="e2s-shot-")
    out = os.path.join(tmpdir, "shot.png")
    profile = os.path.join(tmpdir, "profile")
    proc = None
    try:
        # NOTE: on macOS (Chrome 150) headless writes the --screenshot file in a
        # few seconds but then never exits on its own, so a plain
        # subprocess.run(timeout=45) blocks the full 45s every call. Instead we
        # launch it, poll for the PNG to appear, and kill the process as soon as
        # it does. Linux Chrome exits cleanly and hits the same fast path.
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-extensions", "--hide-scrollbars",
             "--user-data-dir=" + profile,
             "--window-size=%d,%d" % (width, height),
             "--virtual-time-budget=4000",
             "--screenshot=" + out, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                break                      # exited on its own (Linux)
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                time.sleep(0.2)            # let the write flush, then stop waiting
                break
            time.sleep(0.1)
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError("chrome produced no screenshot within 30s")
        with open(out, "rb") as f:
            return f.read()
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "EyeToSpec"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ----------------------------------------------------------
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, abspath):
        if not os.path.isfile(abspath):
            self.send_json({"error": "not found"}, status=404)
            return
        with open(abspath, "rb") as f:
            self.send_bytes(f.read(), content_type_for(abspath))

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path == "/" or path == "":
            return self.send_file(os.path.join(WEB_DIR, "index.html"))

        if path == "/api/packs":
            return self.send_json(list_packs())

        if path.startswith("/api/pack/"):
            pack_id = path[len("/api/pack/"):]
            if not is_safe_id(pack_id):
                return self.send_json({"error": "bad pack id"}, status=400)
            try:
                return self.send_json(read_manifest(pack_id))
            except FileNotFoundError:
                return self.send_json({"error": "pack not found"}, status=404)
            except json.JSONDecodeError as exc:
                return self.send_json({"error": "invalid pack.json: %s" % exc}, status=500)

        # asset files for a pack: /assets/<pack>/<file> where <pack> is one dir
        # ("shop") or a group/frame pair ("guide/f01"). Try the two-segment pack
        # id first (grouped frame), then fall back to the single-segment pack.
        if path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            segs = rel.split("/")
            for depth in (2, 1):
                if len(segs) <= depth:
                    continue
                pack_id = "/".join(segs[:depth])
                if not is_safe_id(pack_id):
                    continue
                config_pack_dir = os.path.join(CONFIG_DIR, *segs[:depth])
                # Live source-backed pack: files live in the game repo's
                # resourceRoot, not the pack's own assets/ dir.
                source = read_source(config_pack_dir)
                if source:
                    try:
                        _, resource_root = build_source_manifest(source)
                    except (OSError, json.JSONDecodeError):
                        continue
                    abspath = os.path.normpath(os.path.join(resource_root, *segs[depth:]))
                    if abspath.startswith(resource_root + os.sep) and os.path.isfile(abspath):
                        return self.send_file(abspath)
                    continue
                pack_dir = os.path.join(config_pack_dir, "assets")
                abspath = os.path.normpath(os.path.join(pack_dir, *segs[depth:]))
                if abspath.startswith(pack_dir + os.sep) and os.path.isfile(abspath):
                    return self.send_file(abspath)
            return self.send_json({"error": "not found"}, status=404)

        # headless screenshot of the render-mode page (agent preview): returns PNG
        if path.startswith("/api/screenshot/"):
            pack_id = path[len("/api/screenshot/"):]
            if not is_safe_id(pack_id):
                return self.send_json({"error": "bad pack id"}, status=400)
            if not os.path.isdir(os.path.join(CONFIG_DIR, pack_id)):
                return self.send_json({"error": "pack not found"}, status=404)
            q = parse_qs(urlparse(self.path).query)
            try:
                width = max(200, min(2000, int(q.get("w", ["720"])[0])))
                height = max(200, min(4000, int(q.get("h", ["1280"])[0])))
            except ValueError:
                width, height = 720, 1280
            safe = q.get("safe", [None])[0]
            capsule = q.get("capsule", [None])[0]
            baseline = q.get("baseline", [None])[0]
            line = q.get("line", [None])[0]
            min_h = q.get("minH", [None])[0]
            try:
                png = render_screenshot(self.server.server_address[1], pack_id, width, height, safe, capsule, baseline, line, min_h)
                return self.send_bytes(png, "image/png")
            except Exception as exc:  # noqa: BLE001
                return self.send_json({"error": "screenshot failed: %s" % exc}, status=500)

        # existing export (so the editor can open on the last saved layout)
        if path.startswith("/api/output/"):
            pack_id = path[len("/api/output/"):]
            if not is_safe_id(pack_id):
                return self.send_json({"error": "bad pack id"}, status=400)
            out = os.path.join(OUTPUT_DIR, pack_id + ".json")
            if os.path.isfile(out):
                return self.send_file(out)
            return self.send_json({}, status=200)

        # static web files
        if "/" not in path[1:]:
            candidate = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
            if candidate.startswith(WEB_DIR + os.sep) and os.path.isfile(candidate):
                return self.send_file(candidate)

        return self.send_json({"error": "not found: %s" % path}, status=404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)

        if path.startswith("/api/save/"):
            pack_id = path[len("/api/save/"):]
            if not is_safe_id(pack_id):
                return self.send_json({"error": "bad pack id"}, status=400)
            if not os.path.isdir(os.path.join(CONFIG_DIR, pack_id)):
                return self.send_json({"error": "unknown pack"}, status=404)
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "invalid json body"}, status=400)
            out = os.path.join(OUTPUT_DIR, pack_id + ".json")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            rel = os.path.relpath(out, ROOT)
            return self.send_json({"ok": True, "path": rel})

        # Write-back: overwrite pack.json with the editor's merged manifest and
        # clear the now-redundant output overlay. Unlike /api/save this DOES
        # modify the hand-authored source — only ever on an explicit user action.
        if path.startswith("/api/writepack/"):
            pack_id = path[len("/api/writepack/"):]
            if not is_safe_id(pack_id):
                return self.send_json({"error": "bad pack id"}, status=400)
            pack_dir = os.path.join(CONFIG_DIR, pack_id)
            if not os.path.isdir(pack_dir):
                return self.send_json({"error": "unknown pack"}, status=404)
            length = int(self.headers.get("Content-Length", 0))
            try:
                manifest = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "invalid json body"}, status=400)
            if not isinstance(manifest, dict) or "elements" not in manifest:
                return self.send_json({"error": "not a pack manifest"}, status=400)
            # Source-bound pack: write the edited placements straight back into the
            # live game layout (source.json is a link, not a snapshot). Never make
            # a pack.json here — one would shadow the source and re-introduce drift.
            source = read_source(pack_dir)
            out = os.path.join(OUTPUT_DIR, pack_id + ".json")
            if source:
                try:
                    layout_path, added = write_source_manifest(source, manifest)
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    return self.send_json({"error": "write-back failed: %s" % e}, status=500)
                if os.path.isfile(out):
                    os.remove(out)  # overlay folded into source; drop it
                return self.send_json({"ok": True, "path": layout_path, "source": True, "added": added})
            # Standalone pack: overwrite its pack.json (hand-authored snapshot).
            pack_path = os.path.join(pack_dir, "pack.json")
            with open(pack_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
                f.write("\n")
            # the overlay is now folded into the pack; drop it so it can't shadow
            if os.path.isfile(out):
                os.remove(out)
            return self.send_json({"ok": True, "path": os.path.relpath(pack_path, ROOT)})

        return self.send_json({"error": "not found"}, status=404)


def main():
    parser = argparse.ArgumentParser(description="EyeToSpec local server")
    parser.add_argument("--port", type=int, default=8770, help="port (default 8770)")
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0, so a phone on your LAN can reach it)")
    parser.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    parser.add_argument("--config", default=None,
                        help="asset-pack directory to serve (default ./config). "
                             "Point it at your own project, e.g. --config ~/game/designs/packs")
    parser.add_argument("--output", default=None,
                        help="where exported coordinate JSON is written (default ./output)")
    args = parser.parse_args()

    global CONFIG_DIR, OUTPUT_DIR
    if args.config:
        CONFIG_DIR = os.path.abspath(os.path.expanduser(args.config))
        if not os.path.isdir(CONFIG_DIR):
            sys.stderr.write("  config dir not found: %s\n" % CONFIG_DIR)
            sys.exit(1)
    if args.output:
        OUTPUT_DIR = os.path.abspath(os.path.expanduser(args.output))
    elif args.config:
        # when serving an external config, default output next to it, not in the tool
        OUTPUT_DIR = os.path.join(CONFIG_DIR, "_eyetospec_output")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://localhost:%d/" % args.port
    listing = list_packs()
    packs = listing["packs"]
    groups = listing["groups"]

    print("\n  EyeToSpec  —  drag it where it looks right, export the coordinates\n")
    print("  serving   %s" % url)
    if args.host == "0.0.0.0":
        print("  phone     http://<your-computer-LAN-ip>:%d/  (same wifi)" % args.port)
    def show(p):
        inside = os.path.commonpath([os.path.abspath(p), ROOT]) == ROOT
        return os.path.relpath(p, ROOT) if inside else p
    print("  config    %s" % show(CONFIG_DIR))
    print("  output    %s" % show(OUTPUT_DIR))
    if packs or groups:
        if packs:
            print("\n  %d pack(s) found:" % len(packs))
            for p in packs:
                print("    - %s  (%d elements)" % (p["id"], p["elementCount"]))
        for g in groups:
            print("\n  group %s  (%d frames):" % (g["id"], len(g["frames"])))
            for f in g["frames"]:
                print("    - %s  (%d elements)" % (f["id"], f["elementCount"]))
    else:
        print("\n  no packs found — drop a folder with a pack.json into ./config")
    print("\n  Ctrl+C to stop\n")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")
        httpd.shutdown()


if __name__ == "__main__":
    main()

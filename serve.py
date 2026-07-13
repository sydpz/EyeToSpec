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
import collections
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
    """Build a pack list entry from a dir holding a pack.json. Returns None if
    it is missing or unreadable. `elementCount` counts the contract's keyed
    `elements` object (falls back to a list length for safety)."""
    manifest_path = os.path.join(pack_dir, "pack.json")
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    elems = data.get("elements")
    count = len(elems) if isinstance(elems, (dict, list)) else 0
    has_export = os.path.isfile(os.path.join(OUTPUT_DIR, pack_id + ".json"))
    return {
        "id": pack_id,
        "name": data.get("name", pack_id),
        "description": data.get("description", ""),
        "elementCount": count,
        "exported": has_export,
        "live": False,
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
# New unified absolute-coordinate contract (2026-07-12).
#
# A pack.json now describes ONE shared config, read verbatim by both the game's
# runtime layout library and EyeToSpec. EyeToSpec is a PURE STATIC COMPOSITOR:
# it paints the canvas + every element by absolute normalized coordinates, low
# `depth` first, and NEVER interprets adaptation logic. All runtime-only fields
# live under `runtime` and are ignored here.
#
#   {
#     "canvas":   { "width": 4000, "height": 4000 },
#     "elements": {
#       "phoneFrame": { "type":"frame", "depth":0, "cx":.5,"cy":.5,"w":.18,"h":.4,
#                       "detail": { "aspect":"720x1600" } },
#       "board":     { "type":"image", "depth":10, "cx":.5,"cy":.34,"w":.076,
#                      "detail": { "tex":"loadout-board" } },
#       "egg":       { "type":"text",  "depth":20, "cx":.54,"cy":.33,"w":.02,
#                      "detail": { "text":"350","fontSize":.008,"color":"#fff" } }
#     },
#     "runtime":  { "fitMode":"scroll", "anchors": { "board":"top" } }  // ignored
#   }
#
# `elements` is a keyed OBJECT (the key is the element id). Each element carries
# a `type` (image|text|box|frame), a `depth` (global stacking order, low→high),
# absolute cx/cy/w[/h] (fractions of canvas), and a `type`-specific `detail`.
# ---------------------------------------------------------------------------

# Which type-specific detail fields each element type contributes to the flat
# render node the front-end consumes. Position/size/orientation are common and
# handled separately; these are purely the "what it looks like" fields.
_DETAIL_FIELDS = {
    "image": ("tex", "fit"),
    "text": ("text", "fontSize", "fontFamily", "fontWeight", "color", "align",
             "stroke", "strokeWidth", "shadow", "fill", "alpha"),
    "box": ("fill", "alpha", "radius", "stroke", "strokeWidth"),
    "frame": ("aspect",),
}
# Common (type-agnostic) geometry/orientation fields copied verbatim.
_COMMON_FIELDS = ("x", "y", "w", "h", "rotation", "flipH", "flipV", "alpha",
                  "label", "group")


def _num(v, d):
    return v if isinstance(v, (int, float)) else d


def _resolve_file(el, tex, profiles):
    """Attach the resolved asset file to an element if its tex is known.

    Two ways a `tex` becomes a file:
      - via an asset-profiles map (game configs: key -> scene subdir + format),
      - as a direct filename resolved against the pack's own assets/ (standalone
        packs like the search-home demo: `"tex": "logo.svg"`).
    The direct-filename fallback lets a hand-authored pack reference art without
    an asset-profiles.json."""
    if not tex:
        return
    if tex in profiles:
        scene, fmt = profiles[tex]
        el["file"] = "%s/%s.%s" % (scene, tex, fmt)
    else:
        el["file"] = tex  # direct filename against the pack's assets/


def _file_to_tex(file, profiles):
    """Reverse of _resolve_file: a resolved "scene/key.fmt" path -> its tex key.

    The editor's working state (and its duplicate `_added` entries) carry the
    flattened `file`, not the contract `tex`. Write-back must fold them back into
    a nested `detail.tex`. If no profile maps to this file (hand-authored packs
    that reference art by direct filename), pass the value through unchanged."""
    if not file:
        return file
    for key, (scene, fmt) in profiles.items():
        if "%s/%s.%s" % (scene, key, fmt) == file:
            return key
    return file


# Geometry/orientation keys the editor's diff carries per element (buildOutput).
_GEO_KEYS = ("x", "y", "w", "h", "rotation", "flipH", "flipV", "depth",
             "anchor", "label", "group")
# Identity keys a duplicate (_added) entry can carry, flat. `file` maps back to
# detail.tex; the rest are text/box detail fields sorted by _DETAIL_FIELDS.
_TEXTISH = set(_DETAIL_FIELDS["text"]) | set(_DETAIL_FIELDS["box"])


def _rebuild_element(entry, profiles):
    """A flat diff/_added entry {id, file|text|..., x,y,w,h,...} -> a nested
    contract element {type, depth, x..., detail:{...}}. Inverse of
    _flatten_element: file->detail.tex, text/box fields->detail."""
    spec = collections.OrderedDict()
    if "file" in entry:
        etype = "image"
        detail = {"tex": _file_to_tex(entry["file"], profiles)}
        if "fit" in entry:
            detail["fit"] = entry["fit"]
    elif "text" in entry:
        etype = "text"
        detail = {f: entry[f] for f in _DETAIL_FIELDS["text"] if f in entry}
    else:
        etype = "box"
        detail = {f: entry[f] for f in _DETAIL_FIELDS["box"] if f in entry}
    spec["type"] = etype
    if "depth" in entry:
        spec["depth"] = entry["depth"]
    for k in ("x", "y", "w", "h", "rotation", "flipH", "flipV", "anchor",
              "label", "group"):
        if k in entry and not (k in ("label", "group") and not entry[k]):
            spec[k] = entry[k]
    spec["detail"] = detail
    return spec


def _apply_diff_to_pack(raw, diff, profiles):
    """Overlay the editor's diff (buildOutput) onto the original nested pack.
    Only touches elements the diff mentions; every other field is left intact.
      - geo by id            -> overlay geometry onto existing element
      - geo with enabled:false -> delete that element (real delete on write-back)
      - unknown id (non-delete) -> treat as a new element (rebuild from flat)
      - _added[]             -> rebuild each into elements
      - elasticZone/anchorLine -> overwrite top-level page property"""
    elements = raw.get("elements")
    if not isinstance(elements, dict):
        elements = collections.OrderedDict()
        raw["elements"] = elements
    for key, val in diff.items():
        if key in ("_added", "elasticZone", "anchorLine", "env"):
            continue
        if not isinstance(val, dict):
            continue
        if val.get("enabled") is False:
            elements.pop(key, None)
            continue
        if key in elements and isinstance(elements[key], dict):
            tgt = elements[key]
            for gk in _GEO_KEYS:
                if gk not in val:
                    continue
                # empty group/label = removed in the editor -> drop the key
                # rather than persist a dangling "group":"" on the element.
                if gk in ("group", "label", "anchor") and not val[gk]:
                    tgt.pop(gk, None)
                else:
                    tgt[gk] = val[gk]
            # identity fields (text/color/fontSize/... and file->tex) live in
            # detail, not top-level. Route any the diff carries into detail.
            det = tgt.get("detail")
            if not isinstance(det, dict):
                det = collections.OrderedDict()
                tgt["detail"] = det
            for ik, iv in val.items():
                if ik in _GEO_KEYS or ik == "enabled":
                    continue
                if ik == "file":
                    det["tex"] = _file_to_tex(iv, profiles)
                elif ik in _TEXTISH or ik in ("tex", "fit"):
                    det[ik] = iv
        else:
            elements[key] = _rebuild_element(dict(val, **{"id": key}), profiles)
    for entry in diff.get("_added", []) if isinstance(diff.get("_added"), list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            elements[entry["id"]] = _rebuild_element(entry, profiles)
    for pk in ("elasticZone", "anchorLine"):
        if pk in diff:
            raw[pk] = diff[pk]
    # env: device-chrome. Merge only the sub-keys the diff carries (frame geo +
    # align, safe-band heights); leave wxCapsule and any name/aux fields intact.
    env_diff = diff.get("env")
    if isinstance(env_diff, dict):
        env = raw.get("env")
        if not isinstance(env, dict):
            env = collections.OrderedDict()
            raw["env"] = env
        fdiff = env_diff.get("frame")
        if isinstance(fdiff, dict):
            frame = env.get("frame")
            if not isinstance(frame, dict):
                frame = collections.OrderedDict()
                env["frame"] = frame
            for fk in ("x", "y", "w", "h", "align"):
                if fk in fdiff:
                    frame[fk] = fdiff[fk]
        for band in ("safeTop", "safeBottom"):
            bdiff = env_diff.get(band)
            if isinstance(bdiff, dict) and "h" in bdiff:
                b = env.get(band)
                if not isinstance(b, dict):
                    b = collections.OrderedDict()
                    env[band] = b
                b["h"] = bdiff["h"]
    return raw


def _flatten_element(eid, spec, profiles):
    """One contract element (keyed object entry) -> a flat render node the
    front-end draws: {id, type, depth, x, y, w, [h], <detail fields>, [file]}.

    Pure projection: copies common geometry + type-specific detail verbatim,
    resolves an image/frame `tex` to a `file`. NO coordinate math — absolute
    values pass straight through. Unknown types fall back to a box."""
    etype = spec.get("type")
    if etype not in _DETAIL_FIELDS:
        etype = "box"
    node = {"id": eid, "type": etype, "depth": _num(spec.get("depth"), 0)}
    for fld in _COMMON_FIELDS:
        if fld in spec:
            node[fld] = spec[fld]
    detail = spec.get("detail") if isinstance(spec.get("detail"), dict) else {}
    for fld in _DETAIL_FIELDS[etype]:
        if fld in detail:
            node[fld] = detail[fld]
    if etype in ("image", "frame"):
        _resolve_file(node, detail.get("tex"), profiles)
    return node


def _expand(path):
    return os.path.abspath(os.path.expanduser(path))


def _load_asset_profiles(profiles_path):
    """key -> (scene subdir, format) from a repo's asset-profiles.json. Used only
    when a pack opts into game texture-key resolution via `assetProfiles`."""
    out = {}
    try:
        with open(profiles_path, "r", encoding="utf-8") as f:
            for p in json.load(f):
                out[p.get("key")] = (p.get("scene"), p.get("format", "png"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return out


def pack_repo_asset_root(pack_dir):
    """If a pack.json opts into game texture-key resolution, its images live in
    the game repo (not the pack's own assets/). Return the absolute directory
    those files hang off — `<repo>/<resourceRoot>` — or None if the pack doesn't
    declare both. Used by the /assets route to serve real game art."""
    manifest = os.path.join(pack_dir, "pack.json")
    if not os.path.isfile(manifest):
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    repo, res = raw.get("repo"), raw.get("resourceRoot")
    if isinstance(repo, str) and isinstance(res, str):
        return _expand(os.path.join(repo, res))
    return None


def build_manifest(pack_dir, raw):
    """Turn a new-contract pack.json into the flat render manifest the front-end
    consumes. PURE STATIC COMPOSITOR: elements are flattened and sorted by depth
    (low first = painted first = underneath), then handed off as an array. The
    `runtime` block is dropped entirely — never read here.

    canvas.{width,height} (contract) is emitted as canvas.{w,h} (render space);
    only the aspect ratio matters for on-screen editing, the numbers are for the
    consuming agent. Element order in the array IS paint order, so the front-end
    can stay a dumb top-to-bottom painter."""
    canvas = raw.get("canvas") if isinstance(raw.get("canvas"), dict) else {}
    cw = _num(canvas.get("width"), _num(canvas.get("w"), 720))
    ch = _num(canvas.get("height"), _num(canvas.get("h"), 1280))

    # optional game texture-key resolution: a pack may name an asset-profiles.json
    # (+ a repo root the scene subdirs hang off) to resolve `tex` keys to files.
    profiles = {}
    prof_rel = raw.get("assetProfiles")
    if isinstance(prof_rel, str):
        base = raw.get("repo")
        prof_path = _expand(os.path.join(base, prof_rel)) if isinstance(base, str) \
            else os.path.join(pack_dir, prof_rel)
        profiles = _load_asset_profiles(prof_path)

    raw_elems = raw.get("elements")
    nodes = []
    if isinstance(raw_elems, dict):
        for eid, spec in raw_elems.items():
            if isinstance(spec, dict):
                nodes.append(_flatten_element(eid, spec, profiles))
    # stable sort by depth: equal depths keep insertion (dict) order -> deterministic.
    nodes.sort(key=lambda n: n["depth"])

    # background: optional top-level base image (NOT an element). Resolve its tex
    # to a file the same way elements do, so the front-end can paint it. Absent
    # background (empty-canvas pages: dialogs/panels) passes through as None.
    background = raw.get("background")
    if isinstance(background, dict):
        background = dict(background)
        _resolve_file(background, background.get("tex"), profiles)
        for layer in background.get("layers", []) if isinstance(background.get("layers"), list) else []:
            if isinstance(layer, dict):
                _resolve_file(layer, layer.get("tex"), profiles)

    return {
        "name": raw.get("name", os.path.basename(pack_dir)),
        "description": raw.get("description", ""),
        "canvas": {"w": cw, "h": ch},
        "background": background,
        "elements": nodes,
        # `env` = device-chrome components (phone frame + safe areas + wx capsule).
        # Declarative: whatever is configured gets drawn, absent → not drawn. Passed
        # through verbatim; the game公共库 ignores it (like `runtime`), it's an
        # EyeToSpec-only viewport hint, never a canvas element.
        "env": raw.get("env") if isinstance(raw.get("env"), dict) else None,
    }


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
            raw = json.load(f)
        return build_manifest(pack_dir, raw)
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
            # Preferred: pack id comes from the ?pack= query, and the whole path
            # after /assets/ is the file relative to that pack's asset root. This
            # is how game-config packs (single or grouped id) request real art.
            q_pack = parse_qs(urlparse(self.path).query).get("pack", [None])[0]
            if q_pack and is_safe_id(q_pack):
                config_pack_dir = os.path.join(CONFIG_DIR, *q_pack.split("/"))
                file_segs = segs
                repo_root = pack_repo_asset_root(config_pack_dir)
                if repo_root:
                    abspath = os.path.normpath(os.path.join(repo_root, *file_segs))
                    if abspath.startswith(repo_root + os.sep) and os.path.isfile(abspath):
                        return self.send_file(abspath)
                pack_dir = os.path.join(config_pack_dir, "assets")
                abspath = os.path.normpath(os.path.join(pack_dir, *file_segs))
                if abspath.startswith(pack_dir + os.sep) and os.path.isfile(abspath):
                    return self.send_file(abspath)
            # Fallback: derive pack id from the leading path segment(s) — the old
            # /assets/<pack>/<file> convention for hand-authored grouped frames.
            for depth in (2, 1):
                if len(segs) <= depth:
                    continue
                pack_id = "/".join(segs[:depth])
                if not is_safe_id(pack_id):
                    continue
                config_pack_dir = os.path.join(CONFIG_DIR, *segs[:depth])
                # Game-config packs (assetProfiles + repo + resourceRoot) serve
                # real art from the repo; hand-authored packs use their assets/.
                repo_root = pack_repo_asset_root(config_pack_dir)
                if repo_root:
                    abspath = os.path.normpath(os.path.join(repo_root, *segs[depth:]))
                    if abspath.startswith(repo_root + os.sep) and os.path.isfile(abspath):
                        return self.send_file(abspath)
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
                diff = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "invalid json body"}, status=400)
            if not isinstance(diff, dict):
                return self.send_json({"error": "diff must be an object"}, status=400)
            # Write-back = apply the editor's diff (buildOutput) onto the ORIGINAL
            # nested pack.json. Read disk as base so every untouched field
            # (repo/runtime/env/canvas/assetProfiles + unchanged elements) is
            # preserved verbatim; only diffed elements are changed/deleted/added.
            pack_path = os.path.join(pack_dir, "pack.json")
            try:
                with open(pack_path, "r", encoding="utf-8") as f:
                    raw = json.load(f, object_pairs_hook=collections.OrderedDict)
            except (OSError, json.JSONDecodeError) as e:
                return self.send_json({"error": "cannot read pack.json: %s" % e},
                                      status=500)
            # profiles for file->tex reverse lookup on rebuilt elements
            profiles = {}
            prof_rel = raw.get("assetProfiles")
            if isinstance(prof_rel, str):
                base = raw.get("repo")
                prof_path = _expand(os.path.join(base, prof_rel)) \
                    if isinstance(base, str) else os.path.join(pack_dir, prof_rel)
                profiles = _load_asset_profiles(prof_path)
            _apply_diff_to_pack(raw, diff, profiles)
            # atomic write: temp file in the same dir, then rename over pack.json
            fd, tmp = tempfile.mkstemp(dir=pack_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                os.replace(tmp, pack_path)
            except OSError as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                return self.send_json({"error": "write failed: %s" % e}, status=500)
            # diff is now baked into pack.json; the output/ overlay is redundant.
            overlay = os.path.join(OUTPUT_DIR, pack_id + ".json")
            if os.path.isfile(overlay):
                os.remove(overlay)
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

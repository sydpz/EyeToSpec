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


def _read_pack_entry(pack_id, manifest_path):
    """Build a pack list entry from a pack.json, or None if unreadable."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    has_export = os.path.isfile(os.path.join(OUTPUT_DIR, pack_id + ".json"))
    return {
        "id": pack_id,
        "name": data.get("name", pack_id),
        "description": data.get("description", ""),
        "elementCount": len(data.get("elements", [])),
        "exported": has_export,
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
        manifest = os.path.join(pack_dir, "pack.json")
        if os.path.isfile(manifest):
            entry = _read_pack_entry(name, manifest)
            if entry:
                packs.append(entry)
            continue
        # no pack.json here -> treat as a group of frame subdirs
        meta = read_group(pack_dir)
        frames = []
        for child in sorted(os.listdir(pack_dir)):
            child_manifest = os.path.join(pack_dir, child, "pack.json")
            if os.path.isfile(child_manifest):
                entry = _read_pack_entry(name + "/" + child, child_manifest)
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
    manifest = os.path.join(CONFIG_DIR, pack_id, "pack.json")
    with open(manifest, "r", encoding="utf-8") as f:
        return json.load(f)


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
                pack_dir = os.path.join(CONFIG_DIR, *segs[:depth], "assets")
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
            pack_path = os.path.join(pack_dir, "pack.json")
            with open(pack_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
                f.write("\n")
            # the overlay is now folded into the pack; drop it so it can't shadow
            out = os.path.join(OUTPUT_DIR, pack_id + ".json")
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

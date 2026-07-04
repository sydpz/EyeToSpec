# EyeToSpec — Design Spec

**Date:** 2026-07-04
**Status:** Draft (awaiting owner approval)

## What it is

EyeToSpec is a small, local, zero-dependency web tool that turns a human's
visual judgment of *where UI elements belong* into a precise coordinate JSON —
so an AI coding agent can build the layout exactly, instead of the human
describing positions in words ("move it left a bit, bigger, no—undo").

**Tagline:** *From your eye to a spec — drag it where it looks right, hand your
AI the exact coordinates.*

## Problem

AI coding agents can write UI code but can't *see* pixels. Aligning a layout
today means: screenshot → agent guesses → "move it left" → guesses again. Slow,
imprecise, endless round-trips. The human has the visual judgment; the agent has
the hands. There's no clean channel between them for *position*.

EyeToSpec is that channel: a visual drag surface whose only output is a
normalized coordinate contract the agent can execute against.

## Scope (deliberately narrow)

**In scope:** asset pack in → drag/resize on a canvas → normalized coordinate
JSON out.

**Out of scope (explicitly not built):** how assets are cut/sourced (user's job),
how the exported JSON is wired into code (agent's job), accounts, cloud, upload
UI (v1 uses `config/` placement only), collaboration, plugins.

This narrowness is a feature. The tool stays small, pure, and easy to trust.

## Form factor

- **Backend:** a tiny Python **standard-library-only** server (`serve.py`). It
  exists for exactly two reasons a static page can't cover: (1) scan `config/`
  to list packs, (2) receive the saved coordinate JSON and write it to
  `output/`. Nothing else.
- **Frontend:** plain HTML/CSS/JS, no build step, no framework.
- **Run:** `python3 serve.py` → browser opens → pick a pack → drag → export.
- **Mobile:** because it's a LAN server, a phone on the same network can open it;
  the editor supports both mouse and touch (drag + resize).

## Directory layout

```
eyetospec/
├── README.md              # English, product-framed, painpoint-first, no personal narrative
├── LICENSE                # MIT
├── serve.py               # single entry point, stdlib only
├── config/                # asset packs live here; scanned at startup
│   └── search-home/       # bundled demo pack (copyright-clean)
│       ├── pack.json      # manifest: canvas size, elements, seed positions
│       └── assets/        # self-drawn SVGs
│           ├── logo.svg           # "SearchEngine" wordmark (original, no trademark)
│           ├── searchbar.svg
│           └── button.svg
├── output/                # exported coordinate JSON lands here, one per pack
│   └── .gitkeep
├── web/
│   ├── index.html         # pack list page (pick a pack)
│   ├── editor.html        # drag/resize editor
│   ├── app.js
│   └── style.css
└── docs/
    ├── pack-format.md     # how to author your own pack
    ├── specs/             # this spec
    └── media/             # README GIF/screenshots
```

## Core concepts

**Asset pack** = one `config/<name>/` directory = a canvas + a set of elements
to place. Defined by `pack.json`:

```json
{
  "name": "Search Home",
  "description": "A demo pack — drag the pieces, export the coordinates.",
  "canvas": { "w": 720, "h": 1280 },
  "background": null,
  "elements": [
    { "id": "logo",   "file": "logo.svg",     "cx": 0.5, "cy": 0.22, "w": 0.5 },
    { "id": "search", "file": "searchbar.svg", "cx": 0.5, "cy": 0.42, "w": 0.8 },
    { "id": "btn_search", "file": "button.svg", "cx": 0.38, "cy": 0.55, "w": 0.28 },
    { "id": "btn_lucky",  "file": "button.svg", "cx": 0.62, "cy": 0.55, "w": 0.28 }
  ]
}
```

**Element kinds** (carried over from the proven template):
- **Image element** (`"file": "x.svg"`): scale by width, height follows aspect.
- **Text element** (`"file": null` + `"text"`): renders the real copy WYSIWYG at
  its `fontSize`/`color`/`align`; drag to move, drag corner to reframe wrap box.
- **Code-drawn / box element** (`"file": null`, no text): a labeled placeholder
  box, resized independently in w and h; optional `fill`/`alpha`/`radius`.

**Coordinate output** (flat, normalized 0..1, relative to the pack's canvas):

```json
{
  "logo":   { "cx": 0.50, "cy": 0.20, "w": 0.50, "h": 0.14 },
  "search": { "cx": 0.50, "cy": 0.40, "w": 0.80, "h": 0.07 }
}
```

- `cx`/`cy` — element center as fraction of canvas width/height.
- `w`/`h` — display size as fraction of canvas width/height.
- No grouping/folding — what you drag is exactly what lands in the JSON.

Normalized (not absolute pixels) is what makes the output resolution- and
device-independent — the reason it generalizes beyond any one screen size.

## Data flow

1. User drops an asset pack folder into `config/` (or uses the bundled demo).
2. `python3 serve.py` scans `config/`, opens the browser to the pack list.
3. User picks a pack → editor renders each element at its seed position over the
   canvas.
4. User drags/resizes elements (mouse or touch) until it looks right.
5. Click **Save/Export** → POST to server → server writes
   `output/<pack>.json`.
6. User hands that JSON to their AI agent. (Wiring it into code is out of scope.)

## The bundled demo: "Search Home"

A generic search-engine landing page (universally recognized layout: centered
logo, rounded search bar, two buttons). The logo is an original self-drawn
wordmark reading **"SearchEngine"** — no real trademark, copyright 100% clean.
Chosen because anyone recognizes the layout instantly (zero explanation cost)
and can judge whether alignment is correct against their mental model. Real-world
use ("drop screenshots of any site into `config/`") is shown in docs as a *user*
workflow, keeping the shipped project clean.

## README & marketing

- **Language:** English only.
- **Framing:** product/painpoint-first, generic tool voice. No personal story, no
  mention of what the author builds, no game references.
- **Structure:** one-line value prop → demo GIF at the very top → 30-second
  quickstart (`python3 serve.py`) → how it works (the 3 element kinds, the coord
  format) → how to author your own pack (link `docs/pack-format.md`).

## Demo GIF

A ~30s screen capture of the full loop on the Search Home demo: launch → pick
pack → drag logo/searchbar/buttons into place → click Export → show the
resulting `output/search-home.json`. Placed at the top of the README — for a
visual tool, one good GIF outsells a thousand lines of docs.

## Anti-goals (to avoid the over-engineering trap)

No accounts, no cloud sync, no collaboration, no plugin system, no build
pipeline, no upload UI in v1. The whole appeal is "one folder, one command, one
JSON." Anything heavier dilutes it.

## Tech decisions (owner delegated)

- MIT license.
- Python stdlib `http.server` based `serve.py`; auto-open browser; default port
  chosen high (e.g. 8770) with `--port` override; bind `0.0.0.0` for phone access.
- SVG assets drawn inline/original for the demo.
- Frontend: vanilla JS, single shared `app.js` for both pages, pointer events for
  unified mouse+touch drag/resize.


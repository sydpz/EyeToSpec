<h1 align="center">👁 EyeToSpec</h1>

<p align="center">
  <strong>From your eye to a spec.</strong><br>
  Drag UI elements where they look right, export the exact coordinates, and hand them to your AI coding agent.
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#author-your-own-pack">Author a pack</a> ·
  <a href="LICENSE">MIT</a>
</p>

<p align="center">
  <img src="docs/media/demo.gif" alt="EyeToSpec demo — drag, rotate, flip, export" width="720">
</p>

---

## The problem

AI coding agents write your UI, but they can't *see* it. Aligning a layout turns
into round after round of typing positions in words:

> "move the logo up a bit — no, too far — now center the button — smaller — undo that…"

You have the visual judgment. The agent has the hands. There's no clean channel
between them for **position**.

**EyeToSpec is that channel.** Drag each element on a canvas until it looks
right, hit export, and you get a compact coordinate JSON your agent can build
against — precisely, in one pass.

## Quickstart

No install, no build step. Just Python 3 (already on macOS and most Linux):

```bash
git clone https://github.com/sydpz/EyeToSpec.git
cd EyeToSpec
python3 serve.py
```

Your browser opens to the pack list. Click **Search Home**, drag the logo,
search bar, and button into place, then hit **💾 Save**. The coordinates land
in `output/search-home.json`.

That file is the whole point — feed it to your agent:

> "Lay out these elements using the normalized coordinates in
> `output/search-home.json` (cx/cy are the element center as a fraction of the
> canvas; w/h are size as a fraction of the canvas)."

## How it works

An **asset pack** is a folder in `config/` describing a canvas and the elements
to place on it. EyeToSpec renders them, you arrange them, and it exports where
they ended up — as normalized coordinates.

**Why normalized (0–1) coordinates?** Because they're resolution- and
device-independent. `cx: 0.5` means "horizontally centered" whether the target
is a 720px phone screen or a 4K display. The agent multiplies by the real canvas
size at build time.

```json
{
  "logo":      { "cx": 0.5,  "cy": 0.18, "w": 0.45, "h": 0.13 },
  "searchbar": { "cx": 0.5,  "cy": 0.34, "w": 0.72, "h": 0.06 },
  "btn_search":{ "cx": 0.38, "cy": 0.46, "w": 0.24, "h": 0.05 }
}
```

- `cx` / `cy` — element **center**, as a fraction of canvas width / height.
- `w` / `h` — display **size**, as a fraction of canvas width / height.

Flat and literal: what you drag is exactly what lands in the JSON. No grouping,
no folding, nothing to decode.

### Rotation and flip

Beyond position and size, each element can be **rotated** (drag the top handle)
and **flipped** horizontally or vertically (the ↔ / ↕ toggles in the inspector).
These only appear in the export when they're set, so simple layouts stay clean:

```json
{
  "btn_search": { "cx": 0.3, "cy": 0.52, "w": 0.3, "rotation": 60, "flipH": true, "flipV": true }
}
```

- `rotation` — degrees clockwise (drag snaps to 15°; hold **Shift** for free rotation).
- `flipH` / `flipV` — mirror left↔right / top↔bottom. Only emitted when `true`.

Flip matters when art has a direction: a sprite drawn facing right can't be
*rotated* to face left without turning upside down — it has to be mirrored.

### Three kinds of element

| Kind | In `pack.json` | In the editor |
|------|----------------|---------------|
| **Image** | `"file": "logo.svg"` | scales by width, height locked to aspect |
| **Text** | `"file": null`, `"text": "Sign in"` | renders the real copy (WYSIWYG) at its font size/color |
| **Box** | `"file": null`, no text | a labeled placeholder you size freely (for code-drawn elements) |

### Delete and duplicate (without touching pack.json)

Sometimes the layout has one element too many, or one too few. The editor lets
you fix that on the spot and it survives Save/reload — **`pack.json` stays your
hand-authored source of truth, untouched**:

- **Delete** (inspector button, or `Delete`/`Backspace`) is a *soft delete*: the
  element is flagged `enabled: false` in `output/`, disappears from the canvas,
  but stays in the element list (greyed, struck-through) with a **↺ restore**
  button. Nothing is lost.
- **Duplicate** (inspector button) clones the element — identity (file/text/
  style) and geometry — with a new id (`<id>-copy`), nudged slightly so you can
  see it. Duplicates are stored in `output._added` with their full definition,
  so they reload intact.

On load the element set is `pack.json elements ∪ output._added`, with each
element's `enabled` and geometry overlaid from `output/`.

### Two ways to Save

The Save button is a split control:

- **💾 Save to pack** (default) — folds everything back into `pack.json`:
  deleted elements are removed for good, duplicates become first-class pack
  elements, and geometry is written in. The `output/` overlay is then cleared,
  so `pack.json` is once again the clean single source of truth. Confirms first
  (it edits your hand-authored file) and reloads.
- **▾ → Save diff (incremental)** — writes only `output/<id>.json`
  (geometry + `enabled` + `_added` overlay) and leaves `pack.json` untouched.
  Use this while iterating; write back to the pack when the layout settles.

## Scope

EyeToSpec does one thing: **turn your visual judgment into a coordinate spec.**

It deliberately does **not** cut your assets (that's your job — screenshot,
export, or draw them) and does **not** wire the JSON into your code (that's your
agent's job). One folder in, one JSON out. That narrowness is the point: it
stays small, predictable, and easy to trust.

## Author your own pack

1. Make a folder under `config/`, e.g. `config/login-screen/`.
2. Drop your cut assets in `config/login-screen/assets/`.
3. Write a `pack.json` describing the canvas and elements.
4. Restart `serve.py` — your pack appears in the list.

Want to align a real page? Screenshot any site, slice out the pieces you care
about, drop them in a pack, and rebuild the layout by eye. The exported
coordinates are yours to hand off.

See **[docs/pack-format.md](docs/pack-format.md)** for the full manifest format.

## Use it from your phone

`serve.py` binds to your LAN by default, so you can drag a layout from a phone
on the same wifi:

```bash
python3 serve.py            # note the "phone" URL it prints
```

Open that URL on your phone — drag and resize work with touch.

> **Note:** the default `--host 0.0.0.0` exposes the server to everyone on your
> local network (there's no auth — it's a personal dev tool). That's fine on a
> home/office wifi; on an untrusted network use `--host 127.0.0.1` for local-only
> access, or an SSH tunnel.

## Options

```bash
python3 serve.py --port 8771    # use a different port
python3 serve.py --no-open      # don't auto-open the browser
python3 serve.py --host 127.0.0.1   # local only (no phone access)
```

## Reproducing the demo

The GIF above is scripted, not hand-recorded — see
[`docs/media/record-demo.js`](docs/media/record-demo.js) (Playwright + ffmpeg).
It's dev-only tooling; the tool itself has zero runtime dependencies.

## License

MIT — see [LICENSE](LICENSE).

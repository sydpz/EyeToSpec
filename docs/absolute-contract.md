# The Absolute-Coordinate Contract

This is the layout contract EyeToSpec renders and your game/app consumes. It is a
**single source of truth**: the editor paints exactly what the file says, and your
runtime reads the same file. No value is re-derived on either side.

Two ideas make it work:

1. **The canvas is a board.** A `canvas` of `4000 × 4000` is a 4000-by-4000 pixel
   board. Everything on it is placed in **absolute pixels** — no `0.xxx`
   fractions anywhere in the file.
2. **EyeToSpec is a pure static compositor.** It reads `canvas` + `elements`,
   sorts by `depth`, and paints low-to-high. That's it. No projection, no
   anchoring math, no viewport derivation. Adaptation to real screens is the
   runtime's job (see [Who does what](#who-does-what)).

## Design decisions (the load-bearing ones)

### Pixels, not fractions

Coordinates are **absolute pixels on the canvas**. `x: 545, y: 90, w: 155` means
"155px wide, its left-top corner at (545, 90)". You can read a layout at a glance
and diff two files meaningfully.

Fractions were removed on purpose. A fraction has to be multiplied by *some*
basis, and when different elements picked different bases (screen width vs screen
height vs canvas height) the numbers silently drifted. Pixels have one basis: the
board.

> Normalizing for different real screens still happens — but in the **runtime
> library**, not in this file and not in EyeToSpec. See below.

### Top-left origin

Every rectangle — elements and env chrome alike — is positioned by its
**top-left corner** (`x`, `y`), not its center.

- It matches how you actually place art: "the sprite's top-left lands here."
- It matches CSS/DOM (`left`/`top`), iOS/Android view frames, SVG `<rect>`, and
  the X/Y a design tool's inspector shows. EyeToSpec is an authoring tool, so it
  speaks the authoring convention.
- Resizing keeps the top-left pinned and grows down-right — predictable, no
  "the center didn't move but the box looks shifted" confusion.

Game engines (Phaser, Unity, Cocos) place sprites by their **center/pivot**
because that's best for rotation and scaling. That's a runtime concern: the
consuming library converts top-left → center (`centerX = x + w/2`). The file
stays top-left.

> Rotation in the editor still spins around the element's center
> (`transform-origin: center`); the origin convention only governs *placement*.

### `depth`, not layers

Each element has one integer `depth`. Lower `depth` is painted first (underneath);
higher sits on top. Ties break by insertion order in the `elements` object. One
flat global paint order — no separate layer/z split.

### Three top-level blocks, three audiences

| Block | EyeToSpec | Runtime (game) |
|-------|-----------|----------------|
| `elements` | renders (depth-sorted) | reads + places |
| `env`      | renders (device chrome) | **ignores** |
| `runtime`  | **ignores** | reads (adaptation, anchors, fit mode) |

`elements` is the shared truth. `env` is EyeToSpec-only scaffolding. `runtime` is
game-only behavior. Each side skips the block that isn't theirs, so the three
never collide.

## File shape

```json
{
  "name": "Loadout (live · absolute px)",
  "description": "…",
  "canvas": { "width": 720, "height": 2200 },

  "assetProfiles": "apps/web-client/asset-profiles.json",
  "repo": "/abs/path/to/game-repo",
  "resourceRoot": "apps/web-client/public/assets",

  "elements": {
    "slot1": {
      "type": "image", "depth": 10,
      "x": 56, "y": 320, "w": 132, "h": 128,
      "detail": { "tex": "loadout-slot" }
    }
  },

  "env": {
    "frame":      { "x": 0, "y": 0, "w": 720, "h": 1600 },
    "safeTop":    { "h": 112, "name": "safe top 7%" },
    "safeBottom": { "h": 64,  "name": "safe bottom" },
    "wxCapsule":  { "name": "WeChat no-tap zone", "x": 545, "y": 90, "w": 155, "h": 65, "basisW": 720 }
  },

  "runtime": {
    "fitMode": "scroll",
    "anchors": { "slot1": "top" }
  }
}
```

### `canvas`

`{ "width", "height" }` in pixels. This is the board. It's declared **by the
content** — a tall scroll page is a long strip (e.g. `720 × 2200`), a dialog is
its own small board. EyeToSpec never invents this number; it renders whatever the
file says and scales the whole board to fit the browser window (display-only —
the stored numbers stay in canvas px).

### `elements`

A **keyed object** (not an array) — the key is the element id, and key insertion
order is the depth tie-breaker.

Every element:

| Field | Meaning |
|-------|---------|
| `type` | `image` \| `text` \| `box` \| `frame` |
| `depth` | integer paint order (low = underneath) |
| `x`, `y` | **top-left** corner in canvas px |
| `w`, `h` | size in canvas px (`h` optional for images — natural aspect locks it) |
| `rotation` | degrees clockwise (optional; spins around center) |
| `flipH` / `flipV` | mirror (optional) |
| `label` | layer tag (optional, single string). A grouping annotation — e.g. `"overlay"` vs `"scroll"` — that marks which layer an element belongs to when several are composited onto one board. EyeToSpec only stores it and lets you filter the canvas by it; the **runtime** maps a label to a layer role (fixed vs scrollable). Absent = untagged. |
| `detail` | type-specific payload (below) |

**`detail` by type**

- `image` — `{ "tex": "…" }` (a texture key resolved via `assetProfiles`, or a
  direct filename).
- `text` — `{ "text", "fontSize", "fontFamily", "fontWeight", "color", "align",
  "stroke", "strokeWidth", "shadow", "fill", "alpha" }`. `fontSize` is canvas px.
- `box` — `{ "fill", "alpha", "radius", "stroke", "strokeWidth" }` for
  code-drawn plates.
- `frame` — `{ }`; but prefer declaring the phone frame under `env`, not here.

### `env` — device chrome

Declarative viewport scaffolding EyeToSpec draws and the game ignores. **Whatever
is configured is drawn; anything absent is not.** Everything hangs off `frame` —
with no `frame`, nothing in `env` is drawn (safe areas and a capsule are
meaningless without a screen to hang them on).

| Component | Fields | Meaning |
|-----------|--------|---------|
| `frame` | `x`, `y`, `w`, `h` (px, top-left) | The phone viewport rectangle, placed **freely** on the board. It does **not** have to sit at the top — a long strip's frame is the first screen-height slice, with scroll content below it. |
| `safeTop` / `safeBottom` | `h` (px, of the frame height), optional `name` | Translucent unsafe bands at the top/bottom **inside the frame**. The label shows the name so you can tell a 7% band from a 5% one at a glance. |
| `wxCapsule` | `x`, `y`, `w`, `h`, `basisW`, optional `name` | WeChat's menu capsule (the forward/close pill, top-right, un-tappable). It is a **physically fixed size** — WeChat lays it out by width, so it scales by the frame's **width** factor (`frame.w / basisW`) and never by screen height. Using a height fraction here would drift between a 1280-tall and a 1600-tall screen. |

### `runtime` — game-only

Ignored by EyeToSpec entirely. Holds adaptation behavior the game engine needs:
`fitMode` (`elastic` \| `scroll`), per-element `anchors`, elastic-zone hints, etc.
None of it affects how the editor paints.

### Asset resolution (optional)

A pack can serve real game art instead of bundling its own:

- `assetProfiles` — path (relative to `repo`) to an `asset-profiles.json` that
  maps texture keys to files.
- `repo` + `resourceRoot` — the game repo root and the assets dir under it. An
  element's `detail.tex` resolves to `<repo>/<resourceRoot>/<scene>/<tex>.<fmt>`;
  the `/assets/…?pack=<id>` route serves the file straight from the repo.

## Who does what

```
EyeToSpec:  reads canvas + elements + env   →  static px compositing
Game lib:   reads canvas + elements + runtime
              →  top-left → center  (x + w/2)
              →  canvas px → screen  (fit / anchor / scroll)
              →  hand to Phaser
```

The **only** transforms live in the game's runtime library, behind unit tests.
The file — and EyeToSpec — stay in absolute canvas pixels. That's the whole point:
one authoritative set of numbers, zero re-derivation, no drift.

## Migrating from the old (normalized) format

The old contract stored fractions (`cx`/`cy`/`w` in `0..1`, center origin) and
split base/overlay packs with runtime-derived `overlayTop`. Translation is pure
arithmetic:

- `x = cx_frac × basis − 0` … actually `x = (cx_center − w/2) × basis` — convert
  center-fraction to top-left-px in one step (multiply by the basis, then shift
  by half the width/height).
- Bake any runtime-derived offset (e.g. `overlayTop = deployBottom + gap`) into
  the absolute `y` **once**, at migration time. The runtime never re-derives it.

Do the multiplication in the generator, not by hand — reading the *values* of the
old JSON (never comments, which may hold pre-tuning drafts) keeps it lossless.

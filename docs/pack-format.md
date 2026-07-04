# Pack format

An **asset pack** is a folder under `config/` that describes a canvas and the
elements you want to place on it. EyeToSpec scans `config/` at startup and lists
every folder that contains a `pack.json`.

```
config/
└── my-pack/
    ├── pack.json        # the manifest (required)
    └── assets/          # image files referenced by elements
        ├── logo.svg
        └── button.svg
```

The exported coordinates are written to `output/my-pack.json`.

## `pack.json`

```json
{
  "name": "My Pack",
  "description": "Short blurb shown on the pack card.",
  "canvas": { "w": 720, "h": 1280 },
  "background": null,
  "elements": [ /* ... */ ]
}
```

| Field | Required | Meaning |
|-------|----------|---------|
| `name` | no | Display name (defaults to the folder name). |
| `description` | no | Shown on the pack card. |
| `canvas` | yes | The reference canvas size in px. All coordinates are normalized against this. Only the **aspect ratio** matters for editing; the numbers matter to your agent. |
| `background` | no | `null` for a plain checkerboard, or `{ "file": "bg.png", "cover": true }` to render a backdrop image. `cover` vs `contain` sets the fit. |
| `elements` | yes | The list of things to place (below). |

## Elements

Every element needs a unique `id` plus **seed** coordinates (`cx`, `cy`, `w`,
and optionally `h`). Seeds are just the starting positions; you drag from there.

Coordinates are normalized `0..1`:

- `cx` / `cy` — element **center** as a fraction of canvas width / height.
- `w` — display **width** as a fraction of canvas width.
- `h` — display **height** as a fraction of canvas height (optional for images).

### Image element

```json
{ "id": "logo", "file": "logo.svg", "cx": 0.5, "cy": 0.2, "w": 0.45 }
```

- `file` — a filename resolved against the pack's `assets/` folder.
- Height follows the image's natural aspect ratio; you scale by dragging the
  corner handle (width drives).
- A missing file renders as a placeholder box, so you can stub a pack before the
  art exists.

### Text element

```json
{
  "id": "cta",
  "file": null,
  "text": "Sign in",
  "fontSize": 22,
  "color": "#3c4043",
  "align": "center",
  "cx": 0.5, "cy": 0.6, "w": 0.4, "h": 0.04
}
```

Renders the **real copy** (WYSIWYG) so a text-heavy screen is edited against the
actual words, not a grey box. `fontSize` is in canvas px (scaled to the on-screen
canvas automatically). Drag to move; drag the corner to reframe the wrap box.

### Box element (code-drawn)

```json
{
  "id": "hpbar",
  "file": null,
  "cx": 0.5, "cy": 0.9, "w": 0.6, "h": 0.04,
  "fill": "#2a1a0c", "alpha": 0.6, "radius": 10
}
```

For things your code draws programmatically (a health bar, a gradient capsule, a
backing plate) — no image file. Renders as a labeled box you size freely in both
width and height. Optional `fill` + `alpha` (0..1) + `radius` (px) paint it in
its real look instead of the generic placeholder.

## Rotation and flip

Any element can also carry an orientation. You set these in the editor (the top
handle rotates; the ↔ / ↕ toggles in the inspector flip) — you rarely write them
by hand — but they can seed a pack too:

```json
{ "id": "arrow", "file": "arrow.svg", "cx": 0.5, "cy": 0.5, "w": 0.2, "rotation": 90, "flipH": true }
```

- `rotation` — degrees clockwise. In the editor, dragging snaps to 15°; hold
  **Shift** for free rotation.
- `flipH` / `flipV` — mirror left↔right / top↔bottom.

In the **export**, `rotation` is only written when non-zero and `flipH` / `flipV`
only when `true`, so orientation-free layouts stay clean. The editor renders each
element with `transform: rotate(<rotation>deg) scale(<flipH?-1:1>, <flipV?-1:1>)`
around its center — match that when consuming the JSON to get the same result.

## Seeding from a previous export

When you open a pack, EyeToSpec loads `output/<pack>.json` if it exists, so you
resume on your last saved layout. Delete that file (or hit **Reset** in the
editor) to go back to the `pack.json` seed positions.

## Wiring the output into code

That's up to you and your agent — EyeToSpec only produces the JSON. Common
patterns:

- **Read at runtime:** `x = coord.cx * canvasWidth`, `y = coord.cy * canvasHeight`.
- **Bake into constants:** copy the numbers into your layout constants.
- **Config-drive:** have the component read the JSON as its layout source.

For a box element, translate `cx/cy/w/h` back into your draw call (e.g. a capsule
centered at `cx*W, cy*H` spanning `w*W × h*H`).

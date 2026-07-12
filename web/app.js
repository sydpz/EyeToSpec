/* EyeToSpec editor — drag & resize elements on a canvas, export normalized coords.
 *
 * Coordinates are normalized 0..1 relative to the pack canvas:
 *   cx, cy = element CENTER as a fraction of canvas width / height
 *   w, h   = element display size as a fraction of canvas width / height
 *
 * Pointer events give us unified mouse + touch (desktop and phone) for free.
 */

'use strict';

const qs = new URLSearchParams(location.search);
const PACK_ID = qs.get('pack');
// Headless RENDER mode (?render=1): hide all editor chrome (topbar/sidebar/
// handles), draw only the canvas + artwork, and expose a `window.__ready` flag
// once layout settles — so a server-side screenshot endpoint can capture a
// clean preview. Purely for agents; the normal editor UI is unaffected.
const RENDER_MODE = qs.get('render') === '1';
// Safe-area overlay (?safe=top:0.07,bottom:0.04): draw translucent red bands over
// the unsafe top/bottom strips and flag any element whose bounding box crosses a
// safe line. Fractions are of canvas HEIGHT (top measured from the top edge,
// bottom from the bottom edge). Absent → no overlay, editor unchanged.
// ?safe= wins; else falls back to the manifest's `safe` block (from the config's
// _eyetospec.safeArea) once the pack loads. `let` so the manifest can seed it.
let SAFE = parseSafe(qs.get('safe'));
// Menu-capsule forbidden zone (?capsule=1): draw the WeChat forward/close capsule
// bounding rect (top-right, unmovable) so HUD elements can be dragged clear of it.
// Rect is expressed on the 720-wide design basis, normalized to canvas here.
// Absent → not drawn, editor unchanged.
// ?capsule=1 forces it on; else the manifest's showCapsule (from _eyetospec) seeds
// it once the pack loads. `let` so the manifest can turn it on.
let CAPSULE = qs.get('capsule') === '1';
// Baseline line semantics (?line=..): the draggable horizontal line means two
// different things depending on the page, and must export different fields:
//   bgAnchor (default) — foreground art pins to background art (battle fence↔barn,
//              home hen↔nest). Exports anchorLine.cy.
//   divider  — top of the elastic content zone (henhouse / shop). Below it,
//              content uses min-height + scroll / stretch. Exports
//              elasticZone.{topCy, minH}.
// The two are NOT interchangeable; the implementer reads a different field.
const LINE_KIND = qs.get('line') === 'divider' ? 'divider' : 'bgAnchor';
// For divider lines: the elastic zone's min-height (fraction of screen height).
// Below minH the zone scrolls; above it, items spread. ?minH=0.5 default 0.5.
const ELASTIC_MIN_H = (() => {
  const n = parseFloat(qs.get('minH'));
  return Number.isFinite(n) ? n : 0.5;
})();

function parseSafe(raw) {
  if (!raw) return null;
  const out = { top: 0, bottom: 0 };
  for (const part of raw.split(',')) {
    const [k, v] = part.split(':');
    const n = parseFloat(v);
    if ((k === 'top' || k === 'bottom') && Number.isFinite(n)) out[k] = n;
  }
  return (out.top > 0 || out.bottom > 0) ? out : null;
}

// Menu-capsule rect on the 720×1280 design basis (from wx.getMenuButtonBoundingClientRect
// measurements): x 545..700, y 90..155. Normalized against a 720×1280 canvas so it
// scales to whatever pack canvas is loaded. cx/cy are the rect's fractional edges.
const CAPSULE_RECT_720 = { x0: 545, x1: 700, y0: 90, y1: 155, basisW: 720, basisH: 1280 };

const stageEl = document.getElementById('stage');
const canvasEl = document.getElementById('canvas');
const listEl = document.getElementById('element-list');
const labelFilterEl = document.getElementById('label-filter');
const inspectorEl = document.getElementById('inspector');
if (labelFilterEl) labelFilterEl.addEventListener('change', () => {
  labelFilter = labelFilterEl.value;
  renderElements();
  renderList();
});
const toastEl = document.getElementById('toast');

let manifest = null;      // the pack.json
let elements = [];        // working state: [{id, file, text, ..., cx, cy, w, h}]
let nodes = new Map();    // id -> DOM node
let selectedIds = new Set();  // multi-select; single-select is a set of one
// The "primary" selection (last clicked) drives the single-element inspector.
let primaryId = null;
// Layer filter: when non-empty, only elements whose `label` equals it are shown
// (canvas + list). '' = show all. Driven by the #label-filter dropdown.
let labelFilter = '';
let canvasAspect = 1;     // w / h of the pack canvas
let zoom = 1;             // 1 = fit-to-viewport; >1 = magnified (stage-wrap scrolls)
const ZOOM_MIN = 1, ZOOM_MAX = 6, ZOOM_STEP = 1.25;
// Baseline (anchorLine): one draggable horizontal line per pack marking the
// "reference object" row (barn's lower edge / candidate-deploy divider). Seeded
// from a saved export or manifest.anchorLine; exported back as anchorLine.cy.
let anchorCy = null;      // normalized 0..1, or null if this pack has no baseline
let anchorLineEl = null;  // the DOM line node
// Overlay-top guide line (read-only, combine view only): normalized 0..1 or null.
// Server-derived from deployRowBottom + gapTop·W — never dragged, never exported.
let guideCy = null;
let guideLineEl = null;

// ---------------------------------------------------------------------------
// load
// ---------------------------------------------------------------------------
init();

async function init() {
  if (!PACK_ID) {
    document.body.innerHTML = '<p style="padding:2rem">No pack specified.</p>';
    return;
  }
  document.getElementById('pack-id').textContent = PACK_ID;

  try {
    manifest = await fetch('/api/pack/' + encodeURIComponent(PACK_ID)).then(r => r.json());
  } catch (e) {
    toast('Failed to load pack: ' + e, true);
    return;
  }
  if (manifest.error) { toast(manifest.error, true); return; }

  // No explicit ?safe= override → adopt the safe area the config declares.
  if (!SAFE && manifest.safe) SAFE = parseSafe(`top:${manifest.safe.top},bottom:${manifest.safe.bottom}`);
  // No explicit ?capsule=1 → adopt the config's showCapsule flag.
  if (!CAPSULE && manifest.showCapsule) CAPSULE = true;

  document.getElementById('pack-name').textContent = manifest.name || PACK_ID;
  const cw = manifest.canvas?.w || 720;
  const ch = manifest.canvas?.h || 1280;
  canvasAspect = cw / ch;
  document.getElementById('canvas-size').textContent = cw + '×' + ch;

  // seed positions: prefer a previously saved export, else the manifest defaults
  let saved = {};
  try {
    saved = await fetch('/api/output/' + encodeURIComponent(PACK_ID)).then(r => r.json());
  } catch (e) { saved = {}; }

  // Element set = pack.json elements  ∪  output._added (duplicates you made in
  // the editor). Each carries a merged geometry from output, plus an `enabled`
  // flag (soft-delete): output wins over pack, default true. Disabled elements
  // stay in the list (greyed, restorable) but don't render on the canvas.
  // `_added` marks editor-created elements so buildOutput() can round-trip them.
  const added = Array.isArray(saved._added) ? saved._added : [];
  const source = (manifest.elements || []).concat(
    added.map(a => Object.assign({}, a, { _added: true })));

  elements = source.map(el => {
    const s = (el._added ? el : saved[el.id]) || {};
    return Object.assign({}, el, {
      x: num(s.x, el.x, 0),      // top-left corner, canvas px
      y: num(s.y, el.y, 0),
      w: num(s.w, el.w, 100),
      h: num(s.h, el.h, null),   // may be null for aspect-locked images until measured
      rotation: num(s.rotation, el.rotation, 0),  // degrees, clockwise
      flipH: bool(s.flipH, el.flipH, false),  // mirror left-right
      flipV: bool(s.flipV, el.flipV, false),  // mirror top-bottom
      // anchor: which reference an element is pinned to. "baseline" (default) =
      // moves with the pack's baseline/background; "top" = floats to the safe-area
      // top (HUD). Mirrors game-engine anchor systems (Unity RectTransform / Cocos
      // Widget); we currently implement two of the values.
      anchor: str(s.anchor, el.anchor, 'baseline'),
      depth: num(s.depth, el.depth, 0),   // paint order (low = underneath)
      // label: layer tag (single string, e.g. "overlay"/"scroll"). Pure grouping
      // annotation — the runtime maps it to a layer role; EyeToSpec stores + filters.
      label: str(s.label, el.label, ''),
      // soft-delete: output.enabled overrides pack.enabled, default enabled.
      enabled: bool(saved[el.id]?.enabled, el.enabled, true),
    });
  });

  // Baseline seed: saved export wins, else manifest.anchorLine, else ?baseline=<cy>
  // (lets a pack with no baseline yet start one, e.g. ?baseline=0.5), else none.
  const baselineParam = parseFloat(qs.get('baseline'));
  anchorCy = num(saved.anchorLine?.cy, manifest.anchorLine?.cy,
                 Number.isFinite(baselineParam) ? baselineParam : null);

  // Overlay-top guide line (combine view): a read-only marker the server derives
  // from the same formula the game uses (deployRowBottom + gapTop·W). Not saved,
  // not draggable — purely a "candidates lay out below here" reference.
  guideCy = Number.isFinite(manifest.guideLine) ? manifest.guideLine : null;

  applyCanvasBackground();
  drawEnv();          // device-chrome (phone frame + safe areas + wx capsule)
  drawSafeBands();
  drawCapsule();
  drawAnchorLine();
  drawGuideLine();
  layoutStage();
  renderElements();
  setupFrameNav();
  refreshLabelFilter();
  renderList();
  window.addEventListener('resize', layoutStage);
  wireToolbar();
  wireAlignBar();

  // Delete / Backspace soft-deletes the selection (ignored while typing coords).
  window.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea')) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && selectedIds.size) {
      e.preventDefault();
      for (const id of [...selectedIds]) setEnabled(id, false);
    }
  });

  if (RENDER_MODE) {
    // Strip editor chrome so only the canvas + artwork remain, then fit the
    // canvas to the full viewport and flag readiness for the screenshot backend.
    document.body.classList.add('render-mode');
    // Re-fit after chrome is hidden (stage area changed) and after images load.
    requestAnimationFrame(() => {
      layoutStage();
      const imgs = Array.from(document.images);
      Promise.all(imgs.map(im => im.complete ? Promise.resolve()
        : new Promise(r => { im.onload = im.onerror = r; })))
        .then(() => { layoutStage(); setTimeout(() => { window.__ready = true; }, 120); });
    });
  }
}

// Frame navigation: when this pack is a group frame ("<group>/<frame>"), wire
// the Prev/Next buttons to walk the group's ordered frames. Standalone packs
// (no "/") leave the nav hidden. In render mode we skip it (agent preview only).
async function setupFrameNav() {
  const nav = document.getElementById('frame-nav');
  if (!nav || RENDER_MODE) return;
  const slash = PACK_ID.indexOf('/');
  if (slash < 0) return;
  const groupId = PACK_ID.slice(0, slash);

  let groups = [];
  try {
    groups = (await fetch('/api/packs').then(r => r.json())).groups || [];
  } catch (e) { return; }
  const group = groups.find(g => g.id === groupId);
  if (!group || !group.frames.length) return;

  const idx = group.frames.findIndex(f => f.id === PACK_ID);
  if (idx < 0) return;

  document.getElementById('frame-progress').textContent =
    (idx + 1) + ' / ' + group.frames.length;

  const go = (i) => {
    const target = group.frames[i];
    if (target) location.href = 'editor.html?pack=' + encodeURIComponent(target.id);
  };
  const prevBtn = document.getElementById('prev-frame');
  const nextBtn = document.getElementById('next-frame');
  prevBtn.disabled = idx === 0;
  nextBtn.disabled = idx === group.frames.length - 1;
  prevBtn.onclick = () => go(idx - 1);
  nextBtn.onclick = () => go(idx + 1);
  // arrow keys walk frames too (ignored while typing in an input)
  window.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea')) return;
    if (e.key === 'ArrowLeft' && idx > 0) go(idx - 1);
    if (e.key === 'ArrowRight' && idx < group.frames.length - 1) go(idx + 1);
  });

  nav.hidden = false;
}

function num(...vals) {
  for (const v of vals) if (typeof v === 'number' && !isNaN(v)) return v;
  return vals[vals.length - 1];
}

function bool(...vals) {
  for (const v of vals) if (typeof v === 'boolean') return v;
  return vals[vals.length - 1];
}

function str(...vals) {
  for (const v of vals) if (typeof v === 'string' && v) return v;
  return vals[vals.length - 1];
}

function bgUrl(file) {
  return `/assets/${encodeURIComponent(PACK_ID)}/${encodeURIComponent(file)}`;
}

function applyCanvasBackground() {
  // remove any previously-drawn stacked layers
  canvasEl.querySelectorAll('.bg-layer').forEach(n => n.remove());

  // Combine view: several stacked layers (hut head then grass body). Each layer
  // is width-filled and seated at the natural bottom of the previous one (the
  // real runtime cut line); a layer flagged `repeat` tiles down to fill the rest.
  // This keeps the true two-plane stacking visible so the owner can judge the
  // overlay's position against the deploy row — the whole point of combine.
  const layers = manifest.backgrounds;
  if (Array.isArray(layers) && layers.filter(l => l && l.file).length) {
    canvasEl.classList.remove('checker');
    canvasEl.style.backgroundImage = '';
    layers.forEach((bg) => {
      if (!bg || !bg.file) return;
      const div = document.createElement('div');
      div.className = 'bg-layer';
      div.dataset.repeat = bg.repeat ? '1' : '';
      div.style.backgroundImage = `url("${bgUrl(bg.file)}")`;
      div.style.backgroundRepeat = bg.repeat ? 'repeat-y' : 'no-repeat';
      div.style.backgroundPosition = 'top center';
      div.style.backgroundSize = '100% auto';
      canvasEl.appendChild(div);
      // Measure natural aspect so the layer's rendered height (= canvas width ×
      // natural ratio) can seat the next layer at its true bottom seam.
      const probe = new Image();
      probe.onload = () => {
        if (probe.naturalWidth > 0) {
          div.dataset.aspect = probe.naturalWidth / probe.naturalHeight;
          positionBgLayers();
        }
      };
      probe.src = bgUrl(bg.file);
    });
    positionBgLayers();
    return;
  }

  const bg = manifest.background;
  if (bg && bg.file) {
    canvasEl.classList.remove('checker');
    if (bg.fit === 'width-top' || bg.fit === 'width-bottom') {
      // The game draws this bg via fillBackgroundWidth: WIDTH-filled (100%) +
      // anchored + overflow-cropped (never contain-centered). Render a single
      // .bg-layer to match — same model as the combine stacked layers — so the
      // owner sees elements sitting on the bg where they truly land at runtime.
      // A repeating bg (overlay grass body) tiles downward to fill the canvas.
      // width-bottom pins the art to the canvas BOTTOM (crops the TOP) — the
      // baseline-anchored pages (endless) whose tall bg overflows upward.
      const bottomAnchored = bg.fit === 'width-bottom';
      canvasEl.style.backgroundImage = '';
      const div = document.createElement('div');
      div.className = 'bg-layer';
      div.dataset.repeat = bg.repeat ? '1' : '';
      div.style.backgroundImage = `url("${bgUrl(bg.file)}")`;
      div.style.backgroundRepeat = bg.repeat ? 'repeat-y' : 'no-repeat';
      div.style.backgroundPosition = (bottomAnchored ? 'bottom' : 'top') + ' center';
      div.style.backgroundSize = '100% auto';
      div.style.left = '0';
      div.style.width = '100%';
      if (bottomAnchored) {
        // Bottom-pinned: the layer fills the whole canvas and CSS bottom-center
        // background-position shows the art's bottom, cropping the overflow off
        // the top — exactly the game's baseline anchor. Skip positionBgLayers
        // (it re-seats from the top for the stacked/top model).
        div.dataset.bottomAnchored = '1';
        div.style.top = '0';
        div.style.bottom = '0';
        canvasEl.appendChild(div);
      } else {
        // topCy (0..1): the panel scroll-bg tucks its top under the head at the real
        // screen y, not y=0. Stash it so positionBgLayers (which runs on every
        // layout and would otherwise reset top to 0) seats the layer there — the bg
        // peeks just above the guide line, candidates below (scene base+panel model).
        if (Number.isFinite(bg.topCy)) div.dataset.topCy = String(bg.topCy);
        div.style.top = (Number.isFinite(bg.topCy) ? bg.topCy * 100 : 0) + '%';
        div.style.bottom = '0';
        canvasEl.appendChild(div);
        positionBgLayers();
      }
    } else {
      // Other pages (home baseline): the bg is anchorY-centered, art bleeds off
      // top/bottom without side-crop — contain keeps it whole and centered.
      canvasEl.style.backgroundImage = `url("${bgUrl(bg.file)}")`;
      canvasEl.style.backgroundSize = bg.cover ? 'cover' : 'contain';
    }
  } else {
    canvasEl.classList.add('checker');
  }
}

// Re-seat stacked .bg-layer divs so each non-repeat layer sits directly below the
// previous one's rendered (natural-aspect) bottom, and the final/repeat layer
// fills the remainder down to the canvas bottom. Called after each layer image
// loads (natural aspect known) and on resize (canvas width changes).
function positionBgLayers() {
  const divs = Array.from(canvasEl.querySelectorAll('.bg-layer'));
  const cw = canvasEl.clientWidth || 720;
  const ch = canvasEl.clientHeight || 1600;
  // A layer may declare topCy (0..1): its top tucks to that screen fraction
  // instead of stacking from 0 — the panel scroll-bg seats just under the head.
  let top = 0;
  divs.forEach((div, i) => {
    // Bottom-anchored layers (width-bottom) are pinned via top:0/bottom:0 +
    // background-position:bottom; the stacking pass doesn't apply to them.
    if (div.dataset.bottomAnchored === '1') return;
    const tcy = parseFloat(div.dataset.topCy);
    if (i === 0 && Number.isFinite(tcy)) top = tcy * ch;
    div.style.left = '0';
    div.style.width = '100%';
    div.style.top = top + 'px';
    const isLast = i === divs.length - 1;
    const repeats = div.dataset.repeat === '1';
    const aspect = parseFloat(div.dataset.aspect);
    if (repeats || isLast || !Number.isFinite(aspect) || aspect <= 0) {
      // fill the rest of the canvas
      div.style.height = 'auto';
      div.style.bottom = '0';
    } else {
      const dispH = cw / aspect; // width-filled → height from natural aspect
      div.style.bottom = 'auto';
      div.style.height = dispH + 'px';
      top += dispH;
    }
  });
}

// ---------------------------------------------------------------------------
// stage sizing: fit the canvas (preserving aspect) into the available area
// ---------------------------------------------------------------------------
function layoutStage() {
  const wrap = stageEl.parentElement;
  const availW = wrap.clientWidth - 32;
  const availH = wrap.clientHeight - 32;
  // Base "fit" size (zoom = 1), then scale up by the zoom factor. At zoom > 1
  // the canvas overflows stage-wrap, which scrolls (see .stage-wrap overflow).
  let w = availW;
  let h = w / canvasAspect;
  if (h > availH) { h = availH; w = h * canvasAspect; }
  w *= zoom; h *= zoom;
  canvasEl.style.width = w + 'px';
  canvasEl.style.height = h + 'px';
  sizeEnv();
  sizeSafeBands();
  sizeCapsule();
  sizeAnchorLine();
  sizeGuideLine();
  positionBgLayers(); // stacked bg heights are px → recompute on any size change
  // re-place nodes now that px size changed
  for (const el of elements) placeNode(el);
  checkSafeViolations();
}

// Set zoom, keeping the current viewport center fixed, then relayout.
function setZoom(next) {
  const wrap = stageEl.parentElement;
  const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
  if (clamped === zoom) return;
  // fraction of scrollable content currently centered in the viewport
  const cxFrac = (wrap.scrollLeft + wrap.clientWidth / 2) / Math.max(1, wrap.scrollWidth);
  const cyFrac = (wrap.scrollTop + wrap.clientHeight / 2) / Math.max(1, wrap.scrollHeight);
  zoom = clamped;
  layoutStage();
  // restore that same center after the canvas resized
  wrap.scrollLeft = cxFrac * wrap.scrollWidth - wrap.clientWidth / 2;
  wrap.scrollTop = cyFrac * wrap.scrollHeight - wrap.clientHeight / 2;
  const label = document.getElementById('zoom-reset-btn');
  if (label) label.textContent = Math.round(zoom * 100) + '%';
}

function canvasPx() {
  return { w: canvasEl.clientWidth, h: canvasEl.clientHeight };
}

// Display scale: the canvas is a fixed board of `canvas.width × canvas.height`
// PIXELS. On screen it's shrunk to fit the viewport (aspect preserved), so one
// factor maps canvas-px → screen-px. All stored coords are canvas px; only
// rendering multiplies by this. S = displayedWidth / canvas.width.
function dispScale() {
  const cw = (manifest && manifest.canvas && manifest.canvas.w) || canvasEl.clientWidth || 1;
  return (canvasEl.clientWidth || cw) / cw;
}

// ---------------------------------------------------------------------------
// env: device-chrome components (declarative — configured → drawn, absent → not)
//   env.frame       phone viewport rect, free-positioned on the canvas by
//                   cx/cy/w + aspectW/aspectH (w drives, aspect locks h). NOT an
//                   `elements` entry → never selectable, never a game element.
//   env.safeTop     translucent band at the TOP of the frame rect, h = fraction of
//   env.safeBottom  the frame HEIGHT (not canvas). Marks the unsafe notch/home bar.
//   env.wxCapsule   WeChat menu-capsule forbidden zone, cx/cy/w + aspectW/aspectH
//                   given RELATIVE TO THE FRAME RECT (0..1 within the frame).
// The whole group hangs off env.frame: no frame → nothing drawn (safe areas and
// capsule are meaningless without a screen to hang them on).
// ---------------------------------------------------------------------------
let envFrameEl = null;   // the phone-frame border div
let envSafeTopEl = null;
let envSafeBotEl = null;
let envCapsuleEl = null;

// The frame's on-screen rect. env.frame is canvas px, top-left origin
// (x/y/w/h); multiply by display scale to get screen px.
function envFrameRect() {
  const env = manifest && manifest.env;
  if (!env || !env.frame) return null;
  const f = env.frame;
  const S = dispScale();
  return {
    left: num(f.x, null, 0) * S,
    top:  num(f.y, null, 0) * S,
    w:    num(f.w, null, 720) * S,
    h:    num(f.h, null, 1280) * S,
    S,
  };
}

// Create the env nodes once (idempotent per load). Only for what's configured.
function drawEnv() {
  const env = manifest && manifest.env;
  if (!env || !env.frame) return;
  const mk = (cls, labelText) => {
    const d = document.createElement('div');
    d.className = cls;
    if (labelText != null) {
      const lbl = document.createElement('div');
      lbl.className = cls + '-label';
      lbl.textContent = labelText;
      d.appendChild(lbl);
    }
    canvasEl.appendChild(d);
    return d;
  };
  envFrameEl = mk('env-frame', null);
  // Safe-area labels carry an optional `name` and always show the px height so you
  // can tell which band is which at a glance. name ? "name 112px" : "safe top 112px".
  const safeLabel = (spec, fallback) => {
    const px = Math.round(num(spec.h, null, 0)) + 'px';
    return spec.name ? `${spec.name} ${px}` : `${fallback} ${px}`;
  };
  if (env.safeTop && num(env.safeTop.h, null, 0) > 0)
    envSafeTopEl = mk('env-safe env-safe-top', safeLabel(env.safeTop, 'safe top'));
  if (env.safeBottom && num(env.safeBottom.h, null, 0) > 0)
    envSafeBotEl = mk('env-safe env-safe-bottom', safeLabel(env.safeBottom, 'safe bottom'));
  if (env.wxCapsule) envCapsuleEl = mk('env-capsule', env.wxCapsule.name || '不可点击区');
  sizeEnv();
}

// Position/size all env nodes against the live frame rect (called from layoutStage).
function sizeEnv() {
  const rect = envFrameRect();
  if (!rect || !envFrameEl) return;
  const env = manifest.env;
  const px = (v) => v + 'px';
  Object.assign(envFrameEl.style, {
    left: px(rect.left), top: px(rect.top), width: px(rect.w), height: px(rect.h),
  });
  const S = rect.S;   // canvas px → screen px
  if (envSafeTopEl) {
    const h = num(env.safeTop.h, null, 0) * S;   // safeTop.h is canvas px (of frame)
    Object.assign(envSafeTopEl.style, {
      left: px(rect.left), top: px(rect.top), width: px(rect.w), height: px(h),
    });
  }
  if (envSafeBotEl) {
    const h = num(env.safeBottom.h, null, 0) * S;
    Object.assign(envSafeBotEl.style, {
      left: px(rect.left), top: px(rect.top + rect.h - h), width: px(rect.w), height: px(h),
    });
  }
  if (envCapsuleEl) {
    // The WeChat capsule is a PHYSICAL fixed-size rect (≈155×65px at 90px from the
    // top on a 720-wide screen). It does NOT scale with screen HEIGHT — WeChat lays
    // it out by width. So we scale its design px (at basisW) by the frame's WIDTH
    // factor: (frameWidthCanvasPx / basisW), then by display scale. No height-frac
    // → no 1280-vs-1600 drift.
    const c = env.wxCapsule;
    const basisW = num(c.basisW, null, 720);
    const frameWcanvas = num(env.frame.w, null, 720);
    const s = (frameWcanvas / basisW) * S;   // width-only scale, into screen px
    Object.assign(envCapsuleEl.style, {
      left: px(rect.left + num(c.x, null, 545) * s),
      top: px(rect.top + num(c.y, null, 90) * s),
      width: px(num(c.w, null, 155) * s),
      height: px(num(c.h, null, 65) * s),
    });
  }
}

// ---------------------------------------------------------------------------
// safe-area overlay (?safe=top:..,bottom:..)
// ---------------------------------------------------------------------------
let safeBandTop = null;
let safeBandBottom = null;

// Create the two band elements once (idempotent). No-op when ?safe absent.
function drawSafeBands() {
  if (!SAFE) return;
  const mk = (cls, labelText) => {
    const band = document.createElement('div');
    band.className = 'safe-band ' + cls;
    const label = document.createElement('div');
    label.className = 'safe-band-label';
    label.textContent = labelText;
    band.appendChild(label);
    canvasEl.appendChild(band);
    return band;
  };
  if (SAFE.top > 0) safeBandTop = mk('top', `unsafe top ${(SAFE.top * 100).toFixed(1)}%`);
  if (SAFE.bottom > 0) safeBandBottom = mk('bottom', `unsafe bottom ${(SAFE.bottom * 100).toFixed(1)}%`);
}

// Size the bands to the live canvas px height (called from layoutStage).
function sizeSafeBands() {
  if (!SAFE) return;
  const { h: CH } = canvasPx();
  if (!CH) return;
  if (safeBandTop) safeBandTop.style.height = (SAFE.top * CH) + 'px';
  if (safeBandBottom) safeBandBottom.style.height = (SAFE.bottom * CH) + 'px';
}

// Flag any element whose bounding box crosses a safe line. Outlines the node red
// and logs a warning (visible in the headless screenshot console too). Compares
// in NORMALIZED cy±h/2 space so it's resolution-independent.
function checkSafeViolations() {
  if (!SAFE) return;   // legacy query-driven path; new packs use env instead
  const CH = (manifest.canvas && manifest.canvas.h) || 1280;
  const violators = [];
  for (const el of elements) {
    const node = nodes.get(el.id);
    if (!node) continue;
    // element vertical extent as fraction of canvas height (x/y = top-left, px)
    const topEdge = el.y / CH;
    const bottomEdge = (el.y + heightOf(el)) / CH;
    const crossesTop = SAFE.top > 0 && topEdge < SAFE.top;
    const crossesBottom = SAFE.bottom > 0 && bottomEdge > (1 - SAFE.bottom);
    node.classList.toggle('safe-violation', crossesTop || crossesBottom);
    if (crossesTop || crossesBottom) {
      violators.push(`${el.id} (${crossesTop ? 'top' : ''}${crossesTop && crossesBottom ? '+' : ''}${crossesBottom ? 'bottom' : ''})`);
    }
  }
  if (violators.length) {
    console.warn(`[safe-area] ${violators.length} element(s) cross the safe line: ${violators.join(', ')}`);
  } else {
    console.log('[safe-area] all elements within safe area ✓');
  }
}

// ---------------------------------------------------------------------------
// menu-capsule forbidden zone (?capsule=1)
// ---------------------------------------------------------------------------
let capsuleEl = null;

// Normalized capsule rect for the CURRENT canvas aspect. The measured rect is on
// a 720×1280 basis; we keep the fractional edges (x/720, y/1280) so the box scales.
function capsuleFrac() {
  const r = CAPSULE_RECT_720;
  return {
    left: r.x0 / r.basisW, right: r.x1 / r.basisW,
    top: r.y0 / r.basisH, bottom: r.y1 / r.basisH,
  };
}

function drawCapsule() {
  if (!CAPSULE) return;
  capsuleEl = document.createElement('div');
  capsuleEl.className = 'capsule-zone';
  const label = document.createElement('div');
  label.className = 'capsule-label';
  label.textContent = 'menu capsule';
  capsuleEl.appendChild(label);
  canvasEl.appendChild(capsuleEl);
}

function sizeCapsule() {
  if (!capsuleEl) return;
  const { w: CW, h: CH } = canvasPx();
  if (!CW || !CH) return;
  const f = capsuleFrac();
  capsuleEl.style.left = (f.left * CW) + 'px';
  capsuleEl.style.top = (f.top * CH) + 'px';
  capsuleEl.style.width = ((f.right - f.left) * CW) + 'px';
  capsuleEl.style.height = ((f.bottom - f.top) * CH) + 'px';
}

// Does an element's bounding box overlap the capsule rect? (normalized space)
function overlapsCapsule(el, node) {
  if (!CAPSULE) return false;   // legacy query-driven path; new packs use env instead
  const CW = (manifest.canvas && manifest.canvas.w) || 720;
  const CH = (manifest.canvas && manifest.canvas.h) || 1280;
  const f = capsuleFrac();
  const l = el.x / CW, r = (el.x + el.w) / CW;
  const t = el.y / CH, b = (el.y + heightOf(el)) / CH;
  return !(r < f.left || l > f.right || b < f.top || t > f.bottom);
}

// ---------------------------------------------------------------------------
// baseline / anchor line (draggable; exported as anchorLine.cy)
// ---------------------------------------------------------------------------
function drawAnchorLine() {
  if (anchorCy == null) return;
  anchorLineEl = document.createElement('div');
  anchorLineEl.className = 'anchor-line';
  const label = document.createElement('div');
  label.className = 'anchor-label';
  label.textContent = 'baseline';
  const grip = document.createElement('div');
  grip.className = 'anchor-grip';
  anchorLineEl.appendChild(label);
  anchorLineEl.appendChild(grip);
  canvasEl.appendChild(anchorLineEl);
  anchorLineEl.addEventListener('pointerdown', onAnchorDown);
}

function sizeAnchorLine() {
  if (!anchorLineEl) return;
  const { h: CH } = canvasPx();
  if (!CH) return;
  anchorLineEl.style.top = (anchorCy * CH) + 'px';
  const lbl = anchorLineEl.querySelector('.anchor-label');
  if (lbl) {
    lbl.textContent = (LINE_KIND === 'divider' ? 'elastic-zone top cy=' : 'bg-anchor cy=')
      + anchorCy.toFixed(3);
  }
}

// Overlay-top guide line: static, read-only. Same DOM shape as the baseline but
// no grip and no pointer handlers — it's derived, not tunable. Marks where the
// candidate overlay begins so owner lays panel elements BELOW it.
function drawGuideLine() {
  if (guideCy == null) return;
  guideLineEl = document.createElement('div');
  guideLineEl.className = 'guide-line';
  const label = document.createElement('div');
  label.className = 'guide-label';
  guideLineEl.appendChild(label);
  canvasEl.appendChild(guideLineEl);
}

function sizeGuideLine() {
  if (!guideLineEl) return;
  const { h: CH } = canvasPx();
  if (!CH) return;
  guideLineEl.style.top = (guideCy * CH) + 'px';
  const lbl = guideLineEl.querySelector('.guide-label');
  if (lbl) lbl.textContent = 'overlay top (candidates below) cy=' + guideCy.toFixed(3);
}

let anchorDrag = null;
function onAnchorDown(e) {
  e.preventDefault();
  e.stopPropagation();
  anchorLineEl.setPointerCapture(e.pointerId);
  anchorDrag = { pointerId: e.pointerId, startY: e.clientY, startCy: anchorCy, CH: canvasPx().h };
  anchorLineEl.addEventListener('pointermove', onAnchorMove);
  anchorLineEl.addEventListener('pointerup', onAnchorUp);
  anchorLineEl.addEventListener('pointercancel', onAnchorUp);
}
function onAnchorMove(e) {
  if (!anchorDrag) return;
  const dy = (e.clientY - anchorDrag.startY) / anchorDrag.CH;
  anchorCy = clamp(anchorDrag.startCy + dy, 0, 1);
  sizeAnchorLine();
}
function onAnchorUp(e) {
  if (!anchorDrag) return;
  anchorLineEl.removeEventListener('pointermove', onAnchorMove);
  anchorLineEl.removeEventListener('pointerup', onAnchorUp);
  anchorLineEl.removeEventListener('pointercancel', onAnchorUp);
  try { anchorLineEl.releasePointerCapture(anchorDrag.pointerId); } catch (e) {}
  anchorDrag = null;
}

// ---------------------------------------------------------------------------
// render elements
// ---------------------------------------------------------------------------
function renderElements() {
  canvasEl.querySelectorAll('.el').forEach(n => n.remove());
  nodes.clear();
  for (const el of elements) {
    if (el.enabled === false) continue;   // soft-deleted: not on canvas
    if (labelFilter && el.label !== labelFilter) continue;  // layer filter
    const node = document.createElement('div');
    node.className = 'el';
    if (el.anchor === 'top') node.classList.add('is-hud');
    node.dataset.id = el.id;

    if (el.file) {
      const img = document.createElement('img');
      img.src = '/assets/' + encodeURIComponent(PACK_ID) + '/' + encodeURIComponent(el.file);
      img.alt = el.id;
      img.draggable = false;
      img.addEventListener('load', () => {
        // lock aspect for images: derive h from natural ratio if h not set
        if (img.naturalWidth && img.naturalHeight) {
          el._imgAspect = img.naturalWidth / img.naturalHeight;
          if (el.h == null) placeNode(el);
        }
      });
      node.appendChild(img);
    } else if (typeof el.text === 'string') {
      node.classList.add('el-text');
      const span = document.createElement('span');
      span.textContent = el.text;
      span.style.color = el.color || '#e8eaed';
      span.style.textAlign = el.align || 'left';
      if (el.fontFamily) span.style.fontFamily = el.fontFamily;
      if (el.fontWeight) span.style.fontWeight = el.fontWeight;
      if (el.stroke) {
        const sw = el.strokeWidth || 3;
        span.style.webkitTextStroke = sw + 'px ' + el.stroke;
        // paint-order so fill sits above stroke (stroke reads as outline)
        span.style.paintOrder = 'stroke fill';
        span.style.webkitTextStrokeColor = el.stroke;
      } else {
        // ZCOOL KuaiLe (the .el-text font) ships a SINGLE weight, so font-weight:700
        // has no bold face to switch to and the browser won't synth faux-bold —
        // bold looked identical to normal. Fake it with a thin self-colored stroke
        // when bold is on and there's no explicit outline. Mirrors game render intent.
        applyFauxBold(span, el.fontWeight, el.color || '#e8eaed');
      }
      if (el.shadow) span.style.textShadow = el.shadow;
      node.dataset.fontSize = el.fontSize || 16;
      node.appendChild(span);
    } else {
      node.classList.add('el-box');
      if (el.fill) {
        node.style.background = hexAlpha(el.fill, el.alpha);
        if (el.radius) node.style.borderRadius = (el.radius) + 'px';
      }
      const label = document.createElement('span');
      label.className = 'el-box-label';
      label.textContent = el.id;
      node.appendChild(label);
    }

    const handle = document.createElement('div');
    handle.className = 'handle';
    node.appendChild(handle);

    const rot = document.createElement('div');
    rot.className = 'rot-handle';
    node.appendChild(rot);

    node.addEventListener('pointerdown', (e) => onPointerDown(e, el, node, 'move'));
    handle.addEventListener('pointerdown', (e) => onPointerDown(e, el, node, 'resize'));
    rot.addEventListener('pointerdown', (e) => onPointerDown(e, el, node, 'rotate'));

    canvasEl.appendChild(node);
    nodes.set(el.id, node);
    placeNode(el);
  }
}

function placeNode(el) {
  const node = nodes.get(el.id);
  if (!node) return;
  const S = dispScale();

  // All coords are canvas px, top-left origin. Screen px = canvas px × display scale.
  const pxW = el.w * S;
  let pxH;
  if (el.h != null) {
    pxH = el.h * S;
  } else if (el._imgAspect) {
    pxH = (el.w / el._imgAspect) * S;   // aspect-locked image (canvas-px h × S)
  } else {
    pxH = el.w * 0.4 * S;               // provisional until image loads
  }

  node.style.width = pxW + 'px';
  node.style.height = pxH + 'px';
  node.style.left = (el.x * S) + 'px';      // x/y = top-left corner, canvas px
  node.style.top = (el.y * S) + 'px';
  node.style.zIndex = Number.isFinite(el.depth) ? el.depth : 0;  // paint order
  const tf = [];
  if (el.rotation) tf.push('rotate(' + el.rotation + 'deg)');
  if (el.flipH || el.flipV) tf.push('scale(' + (el.flipH ? -1 : 1) + ',' + (el.flipV ? -1 : 1) + ')');
  node.style.transform = tf.join(' ');

  if (node.classList.contains('el-text')) {
    const fs = (parseFloat(node.dataset.fontSize) || 16) * S;
    node.querySelector('span').style.fontSize = fs + 'px';
  }
}

// ---------------------------------------------------------------------------
// drag & resize
// ---------------------------------------------------------------------------
let drag = null;

function onPointerDown(e, el, node, mode) {
  e.preventDefault();
  e.stopPropagation();
  const additive = e.shiftKey || e.metaKey || e.ctrlKey;
  // Additive click only toggles selection — don't start a drag (which would
  // move a single element and feel wrong mid multi-select).
  if (additive) { select(el.id, true); return; }
  if (!selectedIds.has(el.id)) select(el.id);
  node.setPointerCapture(e.pointerId);
  const S = dispScale();
  const rect = node.getBoundingClientRect();
  drag = {
    el, node, mode, pointerId: e.pointerId,
    startX: e.clientX, startY: e.clientY,
    startElX: el.x, startElY: el.y, startW: el.w,
    startH: el.h != null ? el.h : (node.offsetHeight / S),
    startRot: el.rotation || 0,
    centerX: rect.left + rect.width / 2,
    centerY: rect.top + rect.height / 2,
    S,
  };
  if (mode === 'rotate') {
    drag.startAngle = Math.atan2(e.clientY - drag.centerY, e.clientX - drag.centerX) * 180 / Math.PI;
  }
  node.addEventListener('pointermove', onPointerMove);
  node.addEventListener('pointerup', onPointerUp);
  node.addEventListener('pointercancel', onPointerUp);
}

function onPointerMove(e) {
  if (!drag) return;
  // pointer delta in canvas px (screen px ÷ display scale)
  const dx = (e.clientX - drag.startX) / drag.S;
  const dy = (e.clientY - drag.startY) / drag.S;
  const el = drag.el;
  const CW = (manifest.canvas && manifest.canvas.w) || 720;

  if (drag.mode === 'resize') {
    // top-left pinned, grow down-right: width follows the drag directly (not ×2)
    el.w = clamp(drag.startW + dx, 4, CW * 4);
    if (el.file && el._imgAspect) {
      el.h = null; // keep aspect-locked
    } else {
      el.h = clamp(drag.startH + dy, 2, CW * 4);
    }
  } else if (drag.mode === 'rotate') {
    const ang = Math.atan2(e.clientY - drag.centerY, e.clientX - drag.centerX) * 180 / Math.PI;
    let next = drag.startRot + (ang - drag.startAngle);
    next = ((next % 360) + 360) % 360;          // normalize 0..360
    if (!e.shiftKey) {                            // snap to 15° unless Shift held
      const snapped = Math.round(next / 15) * 15;
      if (Math.abs(next - snapped) < 4) next = snapped % 360;
    }
    el.rotation = Math.round(next * 10) / 10;
  } else {
    el.x = drag.startElX + dx;   // top-left corner, canvas px (free to go off-board)
    el.y = drag.startElY + dy;
  }
  placeNode(el);
  updateInspector();
}

function onPointerUp(e) {
  if (!drag) return;
  drag.node.removeEventListener('pointermove', onPointerMove);
  drag.node.removeEventListener('pointerup', onPointerUp);
  drag.node.removeEventListener('pointercancel', onPointerUp);
  try { drag.node.releasePointerCapture(drag.pointerId); } catch (e) {}
  drag = null;
}

// ---------------------------------------------------------------------------
// selection, list, inspector
// ---------------------------------------------------------------------------
// select(id): id=null clears. With `additive` (Shift/Cmd-click) the element is
// toggled in/out of the current selection; otherwise it becomes the sole one.
function select(id, additive) {
  if (id == null) {
    selectedIds.clear();
    primaryId = null;
  } else if (additive) {
    if (selectedIds.has(id) && selectedIds.size > 1) {
      selectedIds.delete(id);
      if (primaryId === id) primaryId = [...selectedIds][selectedIds.size - 1];
    } else {
      selectedIds.add(id);
      primaryId = id;
    }
  } else {
    selectedIds = new Set([id]);
    primaryId = id;
  }
  refreshSelectionUI();
}

// Re-sync highlight / inspector / align bar to the current selection set,
// without changing which ids are selected (used after delete/restore).
function refreshSelectionUI() {
  for (const [nid, node] of nodes) node.classList.toggle('selected', selectedIds.has(nid));
  listEl.querySelectorAll('li').forEach(li =>
    li.classList.toggle('active', selectedIds.has(li.dataset.id)));
  updateInspector();
  updateAlignBar();
}

// ---------------------------------------------------------------------------
// multi-element align / distribute / center-on-canvas
// ---------------------------------------------------------------------------
// Element height in canvas px. Explicit h wins; otherwise measure the live node
// (aspect-locked images) and convert screen px → canvas px via display scale.
function heightOf(el) {
  if (el.h != null) return el.h;
  const node = nodes.get(el.id);
  return node ? (node.offsetHeight / dispScale()) : el.w;
}

function selectedElements() {
  return elements.filter(e => selectedIds.has(e.id));
}

// Apply a mutation to each selected element, then re-place + refresh inputs.
function applyToSelection(fn) {
  const sel = selectedElements();
  if (sel.length < 2) return;
  fn(sel);
  for (const el of sel) placeNode(el);
  updateInspector();
}

// Align edges/centers to the selection's bounding box (Figma-style). All canvas
// px, top-left origin: x/y are corners, size is w/h.
function alignSelection(edge) {
  applyToSelection((sel) => {
    const boxes = sel.map(el => ({ el, w: el.w, h: heightOf(el) }));
    const left   = Math.min(...boxes.map(b => b.el.x));
    const right  = Math.max(...boxes.map(b => b.el.x + b.w));
    const top    = Math.min(...boxes.map(b => b.el.y));
    const bottom = Math.max(...boxes.map(b => b.el.y + b.h));
    for (const b of boxes) {
      if (edge === 'left')    b.el.x = left;
      if (edge === 'right')   b.el.x = right - b.w;
      if (edge === 'hcenter') b.el.x = (left + right) / 2 - b.w / 2;
      if (edge === 'top')     b.el.y = top;
      if (edge === 'bottom')  b.el.y = bottom - b.h;
      if (edge === 'vcenter') b.el.y = (top + bottom) / 2 - b.h / 2;
    }
  });
}

// Distribute so element CENTERS are evenly spaced between the two extremes.
// (Simple, predictable; edge-gap distribution can come later if needed.)
function distributeSelection(axis) {
  applyToSelection((sel) => {
    if (sel.length < 3) return;   // 2 elements are already "evenly spaced"
    const posKey = axis === 'h' ? 'x' : 'y';
    const sizeOf = axis === 'h' ? (el => el.w) : heightOf;
    const center = el => el[posKey] + sizeOf(el) / 2;
    const sorted = [...sel].sort((a, b) => center(a) - center(b));
    const lo = center(sorted[0]), hi = center(sorted[sorted.length - 1]);
    const step = (hi - lo) / (sorted.length - 1);
    sorted.forEach((el, i) => { el[posKey] = (lo + step * i) - sizeOf(el) / 2; });
  });
}

// Center the whole selection on the canvas (moves as a group, keeps relative
// layout) on one axis.
function centerOnCanvas(axis) {
  applyToSelection((sel) => {
    const posKey = axis === 'h' ? 'x' : 'y';
    const sizeOf = axis === 'h' ? (el => el.w) : heightOf;
    const canvasSize = axis === 'h'
      ? ((manifest.canvas && manifest.canvas.w) || 720)
      : ((manifest.canvas && manifest.canvas.h) || 1280);
    const lo = Math.min(...sel.map(el => el[posKey]));
    const hi = Math.max(...sel.map(el => el[posKey] + sizeOf(el)));
    const delta = canvasSize / 2 - (lo + hi) / 2;
    for (const el of sel) el[posKey] += delta;
  });
}

// Show the align bar only when 2+ elements are selected.
function updateAlignBar() {
  const bar = document.getElementById('align-bar');
  if (!bar) return;
  const n = selectedIds.size;
  bar.hidden = n < 2;
  const count = document.getElementById('align-count');
  if (count) count.textContent = n + ' selected';
  const distBtns = bar.querySelectorAll('[data-distribute]');
  distBtns.forEach(b => b.disabled = n < 3);
}

function wireAlignBar() {
  const bar = document.getElementById('align-bar');
  if (!bar) return;
  bar.querySelectorAll('[data-align]').forEach(b =>
    b.addEventListener('click', () => alignSelection(b.dataset.align)));
  bar.querySelectorAll('[data-distribute]').forEach(b =>
    b.addEventListener('click', () => distributeSelection(b.dataset.distribute)));
  bar.querySelectorAll('[data-center]').forEach(b =>
    b.addEventListener('click', () => centerOnCanvas(b.dataset.center)));
}

// ---------------------------------------------------------------------------
// delete (soft) / duplicate
// ---------------------------------------------------------------------------
function uniqueId(base) {
  let id = base, n = 2;
  const taken = new Set(elements.map(e => e.id));
  while (taken.has(id)) { id = base + '-' + n; n++; }
  return id;
}

// Soft-delete: flag enabled=false (kept in list, restorable). Pack elements and
// duplicates alike — a disabled duplicate simply won't render but still exports.
function setEnabled(id, on) {
  const el = elements.find(e => e.id === id);
  if (!el) return;
  el.enabled = on;
  if (!on) {                       // deleting: drop it from the selection
    selectedIds.delete(id);
    if (primaryId === id) primaryId = [...selectedIds].pop() || null;
  }
  renderElements();
  renderList();
  refreshSelectionUI();            // re-sync highlight + inspector + align bar
}

// Duplicate: clone identity + geometry, new id (<id>-copy…), nudged so it's
// visible, marked _added so it round-trips via output._added.
function duplicateElement(id) {
  const src = elements.find(e => e.id === id);
  if (!src) return;
  const copy = Object.assign({}, src, {
    id: uniqueId(src.id + '-copy'),
    x: (src.x || 0) + 20,   // nudge 20px down-right so the copy is visible
    y: (src.y || 0) + 20,
    enabled: true,
    _added: true,
  });
  delete copy._imgAspect;   // let the new node measure its own image
  const idx = elements.findIndex(e => e.id === id);
  elements.splice(idx + 1, 0, copy);
  renderElements();
  renderList();
  select(copy.id);
}

// Rebuild the #label-filter dropdown from the labels currently in use. Keeps the
// active selection if it still exists, else falls back to "All".
function refreshLabelFilter() {
  if (!labelFilterEl) return;
  const labels = Array.from(new Set(
    elements.map(e => (e.label || '').trim()).filter(Boolean))).sort();
  if (labelFilter && !labels.includes(labelFilter)) labelFilter = '';
  labelFilterEl.innerHTML = '<option value="">All layers</option>' +
    labels.map(l => `<option value="${l}">${l}</option>`).join('');
  labelFilterEl.value = labelFilter;
}

function renderList() {
  listEl.innerHTML = '';
  for (const el of elements) {
    if (labelFilter && el.label !== labelFilter) continue;  // layer filter
    const li = document.createElement('li');
    li.dataset.id = el.id;
    const kind = el.file ? 'image' : (typeof el.text === 'string' ? 'text' : 'box');
    const disabled = el.enabled === false;
    if (disabled) li.classList.add('is-disabled');
    li.innerHTML = `<span class="el-name"></span>` +
      (el._added ? '<span class="el-tag">copy</span>' : '') +
      (el.label ? `<span class="el-tag el-label-tag">${el.label}</span>` : '') +
      `<span class="el-kind">${kind}</span>` +
      `<button class="el-toggle" title="${disabled ? 'Restore' : 'Delete'}">${disabled ? '↺' : '🗑'}</button>`;
    li.querySelector('.el-name').textContent = el.id;
    const addit = (e) => e.shiftKey || e.metaKey || e.ctrlKey;
    li.querySelector('.el-name').addEventListener('click', (e) => select(el.id, addit(e)));
    li.querySelector('.el-toggle').addEventListener('click', (e) => {
      e.stopPropagation();
      setEnabled(el.id, disabled);   // toggle delete / restore
    });
    li.addEventListener('click', (e) => { if (e.target === li) select(el.id, addit(e)); });
    listEl.appendChild(li);
  }
}

function updateInspector() {
  const el = elements.find(e => e.id === primaryId);
  if (!el) { inspectorEl.innerHTML = '<div class="inspector-empty">Click an element to inspect it.</div>'; return; }
  const h = el.h != null ? el.h : (el._imgAspect ? el.w / el._imgAspect : heightOf(el));
  inspectorEl.innerHTML = `
    <div class="insp-id"></div>
    <div class="insp-grid">
      <label>x<input data-k="x" type="number" step="1" value="${Math.round(el.x)}"></label>
      <label>y<input data-k="y" type="number" step="1" value="${Math.round(el.y)}"></label>
      <label>w<input data-k="w" type="number" step="1" value="${Math.round(el.w)}"></label>
      <label>h<input data-k="h" type="number" step="1" value="${Math.round(h)}"></label>
      <label>rotation°<input data-k="rotation" type="number" step="1" value="${(el.rotation || 0).toFixed(1)}"></label>
      <label>depth<input data-k="depth" type="number" step="1" value="${Number.isFinite(el.depth) ? el.depth : 0}"></label>
    </div>
    ${typeof el.text === 'string' ? `
    <div class="insp-text">
      <label class="insp-text-content">text<input id="insp-text" type="text" value="${escAttr(el.text)}"></label>
      <label class="insp-text-size">size px<input id="insp-fontsize" type="number" step="1" min="1" value="${el.fontSize || 16}"></label>
      <button id="insp-bold" class="bold-btn${Number(el.fontWeight) >= 700 || el.fontWeight === 'bold' ? ' on' : ''}" title="Toggle bold">B</button>
    </div>
    <div class="insp-textstyle">
      <div class="insp-align">
        <button data-textalign="left" class="talign-btn${(el.align || 'left') === 'left' ? ' on' : ''}" title="Align left">⬅</button>
        <button data-textalign="center" class="talign-btn${el.align === 'center' ? ' on' : ''}" title="Align center">⬌</button>
        <button data-textalign="right" class="talign-btn${el.align === 'right' ? ' on' : ''}" title="Align right">➡</button>
      </div>
      <label class="insp-color">color<span class="insp-color-row"><input id="insp-color" type="color" value="${/^#[0-9a-fA-F]{6}$/.test(el.color || '') ? el.color : '#ffffff'}"><input id="insp-color-hex" type="text" spellcheck="false" value="${escAttr(el.color || '#ffffff')}"></span></label>
    </div>` : ''}
    <div class="insp-flip">
      <button data-flip="flipH" class="flip-btn${el.flipH ? ' on' : ''}">↔ flip H</button>
      <button data-flip="flipV" class="flip-btn${el.flipV ? ' on' : ''}">↕ flip V</button>
    </div>
    <div class="insp-anchor">
      <span class="insp-anchor-label">anchor</span>
      <div class="anchor-choices">
        <button data-anchor="baseline" class="anchor-btn${el.anchor === 'baseline' ? ' on' : ''}">baseline</button>
        <button data-anchor="top" class="anchor-btn${el.anchor === 'top' ? ' on' : ''}">top</button>
        <button data-anchor="center" class="anchor-btn${el.anchor === 'center' ? ' on' : ''}">center</button>
        <button data-anchor="bottom" class="anchor-btn${el.anchor === 'bottom' ? ' on' : ''}">bottom</button>
      </div>
    </div>
    <div class="insp-label">
      <label>layer label
        <input id="insp-label" type="text" spellcheck="false" placeholder="e.g. overlay / scroll" value="${escAttr(el.label || '')}">
      </label>
    </div>
    <div class="insp-actions">
      <button id="dup-btn" class="btn btn-ghost" title="Duplicate this element">⧉ Duplicate</button>
      <button id="del-btn" class="btn btn-ghost btn-danger" title="Delete (soft — restorable from the list)">🗑 Delete</button>
    </div>`;
  inspectorEl.querySelector('.insp-id').textContent = el.id;
  const labelInp = inspectorEl.querySelector('#insp-label');
  if (labelInp) labelInp.addEventListener('change', () => {
    el.label = labelInp.value.trim();
    refreshLabelFilter();
    renderList();
    renderElements();   // label may drop el out of / into the active filter
  });
  inspectorEl.querySelector('#dup-btn').addEventListener('click', () => duplicateElement(el.id));
  inspectorEl.querySelector('#del-btn').addEventListener('click', () => setEnabled(el.id, false));
  inspectorEl.querySelectorAll('.anchor-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      el.anchor = btn.dataset.anchor;
      inspectorEl.querySelectorAll('.anchor-btn').forEach(b =>
        b.classList.toggle('on', b.dataset.anchor === el.anchor));
      const node = nodes.get(el.id);
      if (node) node.classList.toggle('is-hud', el.anchor === 'top');
    });
  });
  inspectorEl.querySelectorAll('.flip-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const k = btn.dataset.flip;
      el[k] = !el[k];
      btn.classList.toggle('on', el[k]);
      placeNode(el);
    });
  });
  inspectorEl.querySelectorAll('input[data-k]').forEach(inp => {
    inp.addEventListener('change', () => {
      const k = inp.dataset.k;
      const v = parseFloat(inp.value);
      if (isNaN(v)) return;
      if (k === 'h') { el.h = v; }
      else { el[k] = v; if (k === 'w' && el._imgAspect) el.h = null; }
      placeNode(el);
    });
  });
  // Text content + font size (text elements only). Live-update the rendered
  // span, and mirror fontSize onto the node's data attr so placeNode scales it.
  const txtInp = inspectorEl.querySelector('#insp-text');
  if (txtInp) txtInp.addEventListener('input', () => {
    el.text = txtInp.value;
    const span = nodes.get(el.id)?.querySelector('span');
    if (span) span.textContent = el.text;
  });
  const sizeInp = inspectorEl.querySelector('#insp-fontsize');
  if (sizeInp) sizeInp.addEventListener('change', () => {
    const v = parseFloat(sizeInp.value);
    if (isNaN(v) || v < 1) return;
    el.fontSize = v;
    const node = nodes.get(el.id);
    if (node) { node.dataset.fontSize = v; placeNode(el); }
  });
  // Bold toggle: fontWeight 700 ↔ cleared. Live-update the rendered span, mirror
  // onto the button state; saved through the fontWeight passthrough (IDENTITY_KEYS).
  const boldBtn = inspectorEl.querySelector('#insp-bold');
  if (boldBtn) boldBtn.addEventListener('click', () => {
    const on = !(Number(el.fontWeight) >= 700 || el.fontWeight === 'bold');
    if (on) el.fontWeight = 700; else delete el.fontWeight;
    boldBtn.classList.toggle('on', on);
    const span = nodes.get(el.id)?.querySelector('span');
    if (span) {
      span.style.fontWeight = on ? '700' : '';
      // Keep the faux-bold stroke in sync (only when no explicit outline is set).
      if (!el.stroke) applyFauxBold(span, on ? 700 : undefined, el.color || '#e8eaed');
    }
  });
  // Text align (left/center/right). Saved via IDENTITY_KEYS; live-mirror onto span.
  inspectorEl.querySelectorAll('.talign-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      el.align = btn.dataset.textalign;
      inspectorEl.querySelectorAll('.talign-btn').forEach(b =>
        b.classList.toggle('on', b.dataset.textalign === el.align));
      const span = nodes.get(el.id)?.querySelector('span');
      if (span) span.style.textAlign = el.align;
    });
  });
  // Text color. Saved via IDENTITY_KEYS; live-mirror onto span (keep faux-bold in sync).
  const colorInp = inspectorEl.querySelector('#insp-color');
  const colorHex = inspectorEl.querySelector('#insp-color-hex');
  const applyColor = (hex) => {
    el.color = hex;
    const span = nodes.get(el.id)?.querySelector('span');
    if (span) {
      span.style.color = el.color;
      if (!el.stroke) applyFauxBold(span, el.fontWeight, el.color);
    }
  };
  // Picker → hex text mirror. The hex box is copy/paste-friendly so a color can be
  // lifted off one element and pasted onto others.
  if (colorInp) colorInp.addEventListener('input', () => {
    applyColor(colorInp.value);
    if (colorHex) colorHex.value = colorInp.value;
  });
  if (colorHex) colorHex.addEventListener('input', () => {
    let v = colorHex.value.trim();
    if (v && v[0] !== '#') v = '#' + v;
    if (!/^#[0-9a-fA-F]{6}$/.test(v)) return; // wait for a full valid hex
    applyColor(v);
    if (colorInp) colorInp.value = v;
  });
}

// ZCOOL KuaiLe is single-weight, so font-weight alone can't render bold. Simulate
// bold with a hairline text-stroke in the text's OWN color (thickens each glyph)
// when weight ≥ 700; clear it otherwise. Skip when the element has a real stroke
// (that outline owns webkitTextStroke). Keeps the editor's bold preview honest.
function applyFauxBold(span, fontWeight, color) {
  const on = Number(fontWeight) >= 700 || fontWeight === 'bold';
  if (on) {
    span.style.webkitTextStroke = '0.6px ' + color;
    span.style.webkitTextStrokeColor = color;
  } else {
    span.style.webkitTextStroke = '';
    span.style.webkitTextStrokeColor = '';
  }
}

// Escape a string for safe use inside a double-quoted HTML attribute.
function escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ---------------------------------------------------------------------------
// export / save
// ---------------------------------------------------------------------------
// Fields that define an editor-created (duplicated) element's identity, beyond
// geometry — carried in output._added so a duplicate survives reload without
// touching pack.json.
const IDENTITY_KEYS = ['file', 'text', 'color', 'align', 'fontSize', 'fontFamily',
  'fontWeight', 'stroke', 'strokeWidth', 'shadow', 'fill', 'alpha', 'radius', 'label'];

function buildOutput() {
  const out = {};
  const added = [];
  for (const el of elements) {
    let h = el.h;
    if (h == null) h = heightOf(el);   // measured, in canvas px
    const geo = {
      x: round(el.x), y: round(el.y), w: round(el.w), h: round(h),
    };
    if (el.rotation) geo.rotation = Math.round(el.rotation * 10) / 10;
    if (el.flipH) geo.flipH = true;
    if (el.flipV) geo.flipV = true;
    if (Number.isFinite(el.depth)) geo.depth = el.depth;   // paint order
    if (el.anchor && el.anchor !== 'baseline') geo.anchor = el.anchor;  // default baseline omitted
    if (el.label) geo.label = el.label;   // layer tag, omitted when untagged
    if (el.enabled === false) geo.enabled = false;  // soft-delete flag
    if (el._added) {
      // duplicate: store full definition (identity + geometry) in _added
      const def = { id: el.id };
      for (const k of IDENTITY_KEYS) if (el[k] !== undefined) def[k] = el[k];
      added.push(Object.assign(def, geo));
    } else {
      out[el.id] = geo;
    }
  }
  if (added.length) out._added = added;
  // The horizontal line is a page property (top level), and exports a DIFFERENT
  // field per its kind (§ LINE_KIND):
  //   bgAnchor → anchorLine.cy    (foreground pins to background art)
  //   divider  → elasticZone.{topCy, minH}  (top of the scroll/stretch zone)
  if (anchorCy != null) {
    if (LINE_KIND === 'divider') {
      out.elasticZone = { topCy: round3(anchorCy), minH: round3(ELASTIC_MIN_H) };
    } else {
      out.anchorLine = { cx: 0.5, cy: round3(anchorCy), w: 1, h: 0.04 };
    }
  }
  return out;
}

function round(n) { return Math.round(n); }          // canvas px → integer
function round3(n) { return Math.round(n * 1000) / 1000; }  // fractions (anchor line)

// Build a fresh pack.json from the current working state (for write-back Save).
// Disabled elements are dropped (real delete); duplicates become first-class
// pack elements; geometry is folded in so output/ can be cleared afterwards.
function buildPackManifest() {
  const out = Object.assign({}, manifest);   // preserve name/description/canvas/…
  out.elements = [];
  for (const el of elements) {
    if (el.enabled === false) continue;       // real delete on write-back
    let h = el.h;
    if (h == null) h = heightOf(el);   // measured, canvas px
    // start from the element's own persisted fields, minus editor-internal ones
    const def = {};
    for (const k of Object.keys(el)) {
      if (k === 'enabled' || k === '_added' || k.startsWith('_')) continue;
      def[k] = el[k];
    }
    def.x = round(el.x); def.y = round(el.y); def.w = round(el.w);
    if (h != null) def.h = round(h); else delete def.h;
    delete def.cx; delete def.cy;   // legacy center-origin fields, never re-emit
    if (el.rotation) def.rotation = Math.round(el.rotation * 10) / 10; else delete def.rotation;
    def.flipH = el.flipH || undefined;
    def.flipV = el.flipV || undefined;
    if (def.flipH === undefined) delete def.flipH;
    if (def.flipV === undefined) delete def.flipV;
    if (el.anchor && el.anchor !== 'baseline') def.anchor = el.anchor; else delete def.anchor;
    if (el.label) def.label = el.label; else delete def.label;   // drop empty tag
    out.elements.push(def);
  }
  // fold the baseline/elastic-zone into the manifest too
  if (anchorCy != null) {
    if (LINE_KIND === 'divider') out.elasticZone = { topCy: round(anchorCy), minH: round(ELASTIC_MIN_H) };
    else out.anchorLine = { cx: 0.5, cy: round(anchorCy), w: 1, h: 0.04 };
  }
  return out;
}

function wireToolbar() {
  document.getElementById('save-btn').addEventListener('click', save);
  const menu = document.getElementById('save-menu');
  document.getElementById('save-menu-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
  });
  document.getElementById('save-diff-btn').addEventListener('click', () => {
    menu.hidden = true;
    saveDiff();
  });
  document.addEventListener('click', () => { menu.hidden = true; });   // click-away
  document.getElementById('export-btn').addEventListener('click', showJson);
  document.getElementById('reset-btn').addEventListener('click', resetSeed);
  document.getElementById('close-modal').addEventListener('click', () =>
    document.getElementById('json-modal').hidden = true);
  document.getElementById('copy-btn').addEventListener('click', () => {
    const text = document.getElementById('json-out').textContent;
    navigator.clipboard?.writeText(text).then(() => toast('Copied to clipboard'));
  });
  canvasEl.addEventListener('pointerdown', (e) => { if (e.target === canvasEl) select(null); });

  // Zoom controls: buttons, keyboard (+ / - / 0), and Ctrl/Cmd + wheel.
  document.getElementById('zoom-in-btn').addEventListener('click', () => setZoom(zoom * ZOOM_STEP));
  document.getElementById('zoom-out-btn').addEventListener('click', () => setZoom(zoom / ZOOM_STEP));
  document.getElementById('zoom-reset-btn').addEventListener('click', () => setZoom(1));
  window.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea')) return;
    if (e.key === '+' || e.key === '=') { e.preventDefault(); setZoom(zoom * ZOOM_STEP); }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); setZoom(zoom / ZOOM_STEP); }
    else if (e.key === '0') { e.preventDefault(); setZoom(1); }
  });
  stageEl.parentElement.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;   // plain scroll = pan; Ctrl/Cmd+wheel = zoom
    e.preventDefault();
    setZoom(zoom * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP));
  }, { passive: false });
}

function showJson() {
  document.getElementById('json-out').textContent = JSON.stringify(buildOutput(), null, 2);
  document.getElementById('json-modal').hidden = false;
}

// Default Save = write back to pack.json (fold in deletes/duplicates/geometry,
// then clear the output overlay). This edits the hand-authored source, so it
// confirms first and reloads to the fresh pack afterward.
async function save() {
  const hasDeletes = elements.some(e => e.enabled === false);
  const hasAdds = elements.some(e => e._added);
  const extra = hasDeletes || hasAdds
    ? '\n\nDeleted elements are removed for good; duplicates become permanent.'
    : '';
  if (!confirm('Write these changes back into pack.json?' + extra +
      '\n\nThe output/ overlay for this pack will be cleared. (Use ▾ → Save diff to keep pack.json untouched.)')) {
    return;
  }
  try {
    const res = await fetch('/api/writepack/' + encodeURIComponent(PACK_ID), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPackManifest()),
    }).then(r => r.json());
    if (res.ok) {
      toast('Written → ' + res.path + ' · reloading…');
      setTimeout(() => location.reload(), 600);
    } else {
      toast('Write-back failed: ' + (res.error || '?'), true);
    }
  } catch (e) {
    toast('Write-back failed: ' + e, true);
  }
}

// Incremental Save = write only output/<id>.json (geometry + enabled + _added
// overlay); pack.json stays untouched. The "keep it a diff" option.
async function saveDiff() {
  try {
    const res = await fetch('/api/save/' + encodeURIComponent(PACK_ID), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildOutput()),
    }).then(r => r.json());
    if (res.ok) toast('Saved diff → ' + res.path);
    else toast('Save failed: ' + (res.error || '?'), true);
  } catch (e) {
    toast('Save failed: ' + e, true);
  }
}

function resetSeed() {
  elements = (manifest.elements || []).map(el => Object.assign({}, el, {
    x: num(el.x, 0), y: num(el.y, 0), w: num(el.w, 100),
    h: (typeof el.h === 'number') ? el.h : null,
    rotation: num(el.rotation, 0),
  }));
  renderElements();
  select(null);
  toast('Reset to pack.json seed positions');
}

// ---------------------------------------------------------------------------
// utils
// ---------------------------------------------------------------------------
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function hexAlpha(hex, alpha) {
  if (alpha == null) return hex;
  const m = hex.replace('#', '');
  const r = parseInt(m.substring(0, 2), 16);
  const g = parseInt(m.substring(2, 4), 16);
  const b = parseInt(m.substring(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

let toastTimer = null;
function toast(msg, isError) {
  toastEl.textContent = msg;
  toastEl.classList.toggle('error', !!isError);
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
}

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
const SAFE = parseSafe(qs.get('safe'));
// Menu-capsule forbidden zone (?capsule=1): draw the WeChat forward/close capsule
// bounding rect (top-right, unmovable) so HUD elements can be dragged clear of it.
// Rect is expressed on the 720-wide design basis, normalized to canvas here.
// Absent → not drawn, editor unchanged.
const CAPSULE = qs.get('capsule') === '1';
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
const inspectorEl = document.getElementById('inspector');
const toastEl = document.getElementById('toast');

let manifest = null;      // the pack.json
let elements = [];        // working state: [{id, file, text, ..., cx, cy, w, h}]
let nodes = new Map();    // id -> DOM node
let selectedIds = new Set();  // multi-select; single-select is a set of one
// The "primary" selection (last clicked) drives the single-element inspector.
let primaryId = null;
let canvasAspect = 1;     // w / h of the pack canvas
// Baseline (anchorLine): one draggable horizontal line per pack marking the
// "reference object" row (barn's lower edge / candidate-deploy divider). Seeded
// from a saved export or manifest.anchorLine; exported back as anchorLine.cy.
let anchorCy = null;      // normalized 0..1, or null if this pack has no baseline
let anchorLineEl = null;  // the DOM line node

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

  elements = (manifest.elements || []).map(el => {
    const s = saved[el.id] || {};
    return Object.assign({}, el, {
      cx: num(s.cx, el.cx, 0.5),
      cy: num(s.cy, el.cy, 0.5),
      w: num(s.w, el.w, 0.2),
      h: num(s.h, el.h, null),   // may be null for aspect-locked images until measured
      rotation: num(s.rotation, el.rotation, 0),  // degrees, clockwise
      flipH: bool(s.flipH, el.flipH, false),  // mirror left-right
      flipV: bool(s.flipV, el.flipV, false),  // mirror top-bottom
      // anchor: which reference an element is pinned to. "baseline" (default) =
      // moves with the pack's baseline/background; "top" = floats to the safe-area
      // top (HUD). Mirrors game-engine anchor systems (Unity RectTransform / Cocos
      // Widget); we currently implement two of the values.
      anchor: str(s.anchor, el.anchor, 'baseline'),
    });
  });

  // Baseline seed: saved export wins, else manifest.anchorLine, else ?baseline=<cy>
  // (lets a pack with no baseline yet start one, e.g. ?baseline=0.5), else none.
  const baselineParam = parseFloat(qs.get('baseline'));
  anchorCy = num(saved.anchorLine?.cy, manifest.anchorLine?.cy,
                 Number.isFinite(baselineParam) ? baselineParam : null);

  applyCanvasBackground();
  drawSafeBands();
  drawCapsule();
  drawAnchorLine();
  layoutStage();
  renderElements();
  renderList();
  window.addEventListener('resize', layoutStage);
  wireToolbar();
  wireAlignBar();

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

function applyCanvasBackground() {
  const bg = manifest.background;
  if (bg && bg.file) {
    canvasEl.style.backgroundImage =
      `url("/assets/${encodeURIComponent(PACK_ID)}/${encodeURIComponent(bg.file)}")`;
    canvasEl.style.backgroundSize = bg.cover ? 'cover' : 'contain';
    canvasEl.classList.remove('checker');
  } else {
    canvasEl.classList.add('checker');
  }
}

// ---------------------------------------------------------------------------
// stage sizing: fit the canvas (preserving aspect) into the available area
// ---------------------------------------------------------------------------
function layoutStage() {
  const wrap = stageEl.parentElement;
  const availW = wrap.clientWidth - 32;
  const availH = wrap.clientHeight - 32;
  let w = availW;
  let h = w / canvasAspect;
  if (h > availH) { h = availH; w = h * canvasAspect; }
  canvasEl.style.width = w + 'px';
  canvasEl.style.height = h + 'px';
  sizeSafeBands();
  sizeCapsule();
  sizeAnchorLine();
  // re-place nodes now that px size changed
  for (const el of elements) placeNode(el);
  checkSafeViolations();
}

function canvasPx() {
  return { w: canvasEl.clientWidth, h: canvasEl.clientHeight };
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
  if (!SAFE) return;
  const violators = [];
  for (const el of elements) {
    const node = nodes.get(el.id);
    if (!node) continue;
    const { h: CH } = canvasPx();
    // element's normalized vertical extent (top/bottom edge as fraction of height)
    const halfH = (node.offsetHeight / 2) / (CH || 1);
    const topEdge = el.cy - halfH;
    const bottomEdge = el.cy + halfH;
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
  if (!CAPSULE) return false;
  const { w: CW, h: CH } = canvasPx();
  const halfW = (node.offsetWidth / 2) / (CW || 1);
  const halfH = (node.offsetHeight / 2) / (CH || 1);
  const f = capsuleFrac();
  const l = el.cx - halfW, r = el.cx + halfW, t = el.cy - halfH, b = el.cy + halfH;
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
  const { w: CW, h: CH } = canvasPx();
  if (!CW || !CH) return;

  const pxW = el.w * CW;
  let pxH;
  if (el.h != null) {
    pxH = el.h * CH;
  } else if (el._imgAspect) {
    pxH = pxW / el._imgAspect;   // aspect-locked image
  } else {
    pxH = el.w * CW * 0.4;       // provisional until image loads
  }

  node.style.width = pxW + 'px';
  node.style.height = pxH + 'px';
  node.style.left = (el.cx * CW - pxW / 2) + 'px';
  node.style.top = (el.cy * CH - pxH / 2) + 'px';
  const tf = [];
  if (el.rotation) tf.push('rotate(' + el.rotation + 'deg)');
  if (el.flipH || el.flipV) tf.push('scale(' + (el.flipH ? -1 : 1) + ',' + (el.flipV ? -1 : 1) + ')');
  node.style.transform = tf.join(' ');

  if (node.classList.contains('el-text')) {
    const fs = (parseFloat(node.dataset.fontSize) || 16) * (CW / (manifest.canvas.w || CW));
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
  const { w: CW, h: CH } = canvasPx();
  const rect = node.getBoundingClientRect();
  drag = {
    el, node, mode, pointerId: e.pointerId,
    startX: e.clientX, startY: e.clientY,
    startCx: el.cx, startCy: el.cy, startW: el.w,
    startH: el.h != null ? el.h : (node.offsetHeight / CH),
    startRot: el.rotation || 0,
    centerX: rect.left + rect.width / 2,
    centerY: rect.top + rect.height / 2,
    CW, CH,
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
  const dx = (e.clientX - drag.startX) / drag.CW;
  const dy = (e.clientY - drag.startY) / drag.CH;
  const el = drag.el;

  if (drag.mode === 'resize') {
    el.w = clamp(drag.startW + dx * 2, 0.02, 4);
    if (el.file && el._imgAspect) {
      el.h = null; // keep aspect-locked
    } else {
      el.h = clamp(drag.startH + dy * 2, 0.01, 4);
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
    el.cx = clamp(drag.startCx + dx, -0.5, 1.5);
    el.cy = clamp(drag.startCy + dy, -0.5, 1.5);
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
  for (const [nid, node] of nodes) node.classList.toggle('selected', selectedIds.has(nid));
  listEl.querySelectorAll('li').forEach(li =>
    li.classList.toggle('active', selectedIds.has(li.dataset.id)));
  updateInspector();
  updateAlignBar();
}

// ---------------------------------------------------------------------------
// multi-element align / distribute / center-on-canvas
// ---------------------------------------------------------------------------
// Normalized half-height of an element. Explicit h wins; otherwise measure the
// live node (aspect-locked images) and convert to canvas fraction.
function halfHeightOf(el) {
  if (el.h != null) return el.h / 2;
  const node = nodes.get(el.id);
  const { h: CH } = canvasPx();
  return node ? (node.offsetHeight / CH) / 2 : el.w / 2;
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

// Align edges/centers to the selection's bounding box (Figma-style).
function alignSelection(edge) {
  applyToSelection((sel) => {
    const boxes = sel.map(el => ({
      el, hw: el.w / 2, hh: halfHeightOf(el),
    }));
    const left   = Math.min(...boxes.map(b => b.el.cx - b.hw));
    const right  = Math.max(...boxes.map(b => b.el.cx + b.hw));
    const top    = Math.min(...boxes.map(b => b.el.cy - b.hh));
    const bottom = Math.max(...boxes.map(b => b.el.cy + b.hh));
    for (const b of boxes) {
      if (edge === 'left')   b.el.cx = left + b.hw;
      if (edge === 'right')  b.el.cx = right - b.hw;
      if (edge === 'hcenter') b.el.cx = (left + right) / 2;
      if (edge === 'top')    b.el.cy = top + b.hh;
      if (edge === 'bottom') b.el.cy = bottom - b.hh;
      if (edge === 'vcenter') b.el.cy = (top + bottom) / 2;
    }
  });
}

// Distribute so element CENTERS are evenly spaced between the two extremes.
// (Simple, predictable; edge-gap distribution can come later if needed.)
function distributeSelection(axis) {
  applyToSelection((sel) => {
    if (sel.length < 3) return;   // 2 elements are already "evenly spaced"
    const key = axis === 'h' ? 'cx' : 'cy';
    const sorted = [...sel].sort((a, b) => a[key] - b[key]);
    const lo = sorted[0][key], hi = sorted[sorted.length - 1][key];
    const step = (hi - lo) / (sorted.length - 1);
    sorted.forEach((el, i) => { el[key] = lo + step * i; });
  });
}

// Center the whole selection on the canvas (moves as a group, keeps relative
// layout) on one axis.
function centerOnCanvas(axis) {
  applyToSelection((sel) => {
    const key = axis === 'h' ? 'cx' : 'cy';
    const hwOf = axis === 'h' ? (el => el.w / 2) : halfHeightOf;
    const lo = Math.min(...sel.map(el => el[key] - hwOf(el)));
    const hi = Math.max(...sel.map(el => el[key] + hwOf(el)));
    const delta = 0.5 - (lo + hi) / 2;
    for (const el of sel) el[key] += delta;
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

function renderList() {
  listEl.innerHTML = '';
  for (const el of elements) {
    const li = document.createElement('li');
    li.dataset.id = el.id;
    const kind = el.file ? 'image' : (typeof el.text === 'string' ? 'text' : 'box');
    li.innerHTML = `<span class="el-name"></span><span class="el-kind">${kind}</span>`;
    li.querySelector('.el-name').textContent = el.id;
    li.addEventListener('click', (e) => select(el.id, e.shiftKey || e.metaKey || e.ctrlKey));
    listEl.appendChild(li);
  }
}

function updateInspector() {
  const el = elements.find(e => e.id === primaryId);
  if (!el) { inspectorEl.innerHTML = '<div class="inspector-empty">Click an element to inspect it.</div>'; return; }
  const h = el.h != null ? el.h : (el._imgAspect ? el.w / el._imgAspect * (manifest.canvas.w / manifest.canvas.h) : 0);
  inspectorEl.innerHTML = `
    <div class="insp-id"></div>
    <div class="insp-grid">
      <label>cx<input data-k="cx" type="number" step="0.001" value="${el.cx.toFixed(3)}"></label>
      <label>cy<input data-k="cy" type="number" step="0.001" value="${el.cy.toFixed(3)}"></label>
      <label>w<input data-k="w" type="number" step="0.001" value="${el.w.toFixed(3)}"></label>
      <label>h<input data-k="h" type="number" step="0.001" value="${h.toFixed(3)}"></label>
      <label>rotation°<input data-k="rotation" type="number" step="1" value="${(el.rotation || 0).toFixed(1)}"></label>
    </div>
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
    </div>`;
  inspectorEl.querySelector('.insp-id').textContent = el.id;
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
  inspectorEl.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', () => {
      const k = inp.dataset.k;
      const v = parseFloat(inp.value);
      if (isNaN(v)) return;
      if (k === 'h') { el.h = v; }
      else { el[k] = v; if (k === 'w' && el._imgAspect) el.h = null; }
      placeNode(el);
    });
  });
}

// ---------------------------------------------------------------------------
// export / save
// ---------------------------------------------------------------------------
function buildOutput() {
  const out = {};
  const { w: CW, h: CH } = canvasPx();
  for (const el of elements) {
    let h = el.h;
    if (h == null) {
      const node = nodes.get(el.id);
      h = node ? node.offsetHeight / CH : 0;
    }
    out[el.id] = {
      cx: round(el.cx), cy: round(el.cy), w: round(el.w), h: round(h),
    };
    if (el.rotation) out[el.id].rotation = Math.round(el.rotation * 10) / 10;
    if (el.flipH) out[el.id].flipH = true;
    if (el.flipV) out[el.id].flipV = true;
    if (el.anchor && el.anchor !== 'baseline') out[el.id].anchor = el.anchor;  // default baseline omitted
  }
  // The horizontal line is a page property (top level), and exports a DIFFERENT
  // field per its kind (§ LINE_KIND):
  //   bgAnchor → anchorLine.cy    (foreground pins to background art)
  //   divider  → elasticZone.{topCy, minH}  (top of the scroll/stretch zone)
  if (anchorCy != null) {
    if (LINE_KIND === 'divider') {
      out.elasticZone = { topCy: round(anchorCy), minH: round(ELASTIC_MIN_H) };
    } else {
      out.anchorLine = { cx: 0.5, cy: round(anchorCy), w: 1, h: 0.04 };
    }
  }
  return out;
}

function round(n) { return Math.round(n * 1000) / 1000; }

function wireToolbar() {
  document.getElementById('save-btn').addEventListener('click', save);
  document.getElementById('export-btn').addEventListener('click', showJson);
  document.getElementById('reset-btn').addEventListener('click', resetSeed);
  document.getElementById('close-modal').addEventListener('click', () =>
    document.getElementById('json-modal').hidden = true);
  document.getElementById('copy-btn').addEventListener('click', () => {
    const text = document.getElementById('json-out').textContent;
    navigator.clipboard?.writeText(text).then(() => toast('Copied to clipboard'));
  });
  canvasEl.addEventListener('pointerdown', (e) => { if (e.target === canvasEl) select(null); });
}

function showJson() {
  document.getElementById('json-out').textContent = JSON.stringify(buildOutput(), null, 2);
  document.getElementById('json-modal').hidden = false;
}

async function save() {
  try {
    const res = await fetch('/api/save/' + encodeURIComponent(PACK_ID), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildOutput()),
    }).then(r => r.json());
    if (res.ok) toast('Saved → ' + res.path);
    else toast('Save failed: ' + (res.error || '?'), true);
  } catch (e) {
    toast('Save failed: ' + e, true);
  }
}

function resetSeed() {
  elements = (manifest.elements || []).map(el => Object.assign({}, el, {
    cx: num(el.cx, 0.5), cy: num(el.cy, 0.5), w: num(el.w, 0.2),
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

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

const stageEl = document.getElementById('stage');
const canvasEl = document.getElementById('canvas');
const listEl = document.getElementById('element-list');
const inspectorEl = document.getElementById('inspector');
const toastEl = document.getElementById('toast');

let manifest = null;      // the pack.json
let elements = [];        // working state: [{id, file, text, ..., cx, cy, w, h}]
let nodes = new Map();    // id -> DOM node
let selectedId = null;
let canvasAspect = 1;     // w / h of the pack canvas

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
    });
  });

  applyCanvasBackground();
  layoutStage();
  renderElements();
  renderList();
  window.addEventListener('resize', layoutStage);
  wireToolbar();

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
  // re-place nodes now that px size changed
  for (const el of elements) placeNode(el);
}

function canvasPx() {
  return { w: canvasEl.clientWidth, h: canvasEl.clientHeight };
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
  select(el.id);
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
function select(id) {
  selectedId = id;
  for (const [nid, node] of nodes) node.classList.toggle('selected', nid === id);
  listEl.querySelectorAll('li').forEach(li =>
    li.classList.toggle('active', li.dataset.id === id));
  updateInspector();
}

function renderList() {
  listEl.innerHTML = '';
  for (const el of elements) {
    const li = document.createElement('li');
    li.dataset.id = el.id;
    const kind = el.file ? 'image' : (typeof el.text === 'string' ? 'text' : 'box');
    li.innerHTML = `<span class="el-name"></span><span class="el-kind">${kind}</span>`;
    li.querySelector('.el-name').textContent = el.id;
    li.addEventListener('click', () => select(el.id));
    listEl.appendChild(li);
  }
}

function updateInspector() {
  const el = elements.find(e => e.id === selectedId);
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
    </div>`;
  inspectorEl.querySelector('.insp-id').textContent = el.id;
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

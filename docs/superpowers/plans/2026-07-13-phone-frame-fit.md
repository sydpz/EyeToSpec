# Phone Frame Fit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 EyeToSpec 的 phone frame 加一个侧边栏常驻编辑面板,支持 `align`(top/bottom/center/baseline)运行时适配规范 + 由 align 推导的初始 `y`,并把 `env.frame`/安全区变更写回 pack.json。

**Architecture:** frame 从只读显示升级为可编辑对象,但**不做画布点选/拖拽** —— 只在侧边栏常驻面板里调。`align` 是编辑期真源(规则),`y` 是它在基准画布上烘焙出的 px 结果(只读展示 + 持久化)。写回复用现有 `_apply_diff_to_pack` 机制,新增 `env` 分支。anchorLine 的 cy→px 收口**不在本 plan**(见 spec §8),baseline 用兼容函数读线(cy 或 y 都能读)。

**Tech Stack:** 纯前端 vanilla JS(web/app.js)+ Python http.server(serve.py)。无测试框架 —— 验证走 `node --check` / `python3 -c ast.parse` + ad-hoc Python roundtrip 脚本(与既有 group 特性一致)。

## Global Constraints

- config/EyeToSpec 这层坐标一律 **px + 左上角原点**;归一化/换算是游戏运行时公共库的事,不在这步。
- `manifest.canvas` 用 `.w`/`.h`(从 pack 的 `width`/`height` 映射);pack.json 里是 `canvas.width`/`canvas.height`。
- `env.frame` 字段:`x`/`y`/`w`/`h` 均 px;新增 `align` ∈ {top,bottom,center,baseline},缺省 top。
- 安全区沿用 `env.safeTop.h` / `env.safeBottom.h`(px)。
- baseline 锚点 = **frame 顶边**;`frame.y = anchorLine 的 y`。
- anchorLine 现状存 0–1 `cy`;本 plan **不改**它,用兼容函数 `anchorLineY(canvasH)` 读(cy×H 或直接 y)。
- 写回是原子操作(mkstemp + os.replace),保留所有未 diff 的顶层字段。
- 每个 pack 只有一个 frame;frame 可选(无 env.frame → 面板 disabled)。

---

### Task 1: serve.py 写回支持 env.frame / 安全区

**Files:**
- Modify: `serve.py:208-260`（`_apply_diff_to_pack`，新增 `env` 分支）
- Test: ad-hoc Python roundtrip（临时脚本，验证后删）

**Interfaces:**
- Consumes: 既有 `_apply_diff_to_pack(raw, diff, profiles)`；`raw` 是 OrderedDict 形态的原 pack。
- Produces: diff 里出现 `env` 键时，把 `env.frame` 的 x/y/w/h/align 与 `env.safeTop.h`/`env.safeBottom.h` 合并进 `raw["env"]`；其它 env 子键（wxCapsule 等）保持原样不动。align 空串或 "top" 缺省时仍写入（top 是合法值，不 pop）。

- [ ] **Step 1: 写 roundtrip 失败测试**

创建临时 `/tmp/test_env_writeback.py`：

```python
import collections, json, sys
sys.path.insert(0, "/Users/bingwang/github/EyeToSpec")
import serve

raw = collections.OrderedDict([
    ("canvas", {"width": 720, "height": 1697}),
    ("elements", collections.OrderedDict([
        ("hen", {"type": "image", "x": 255, "y": 1257, "w": 158, "h": 255,
                 "detail": {"tex": "hen-normal"}})
    ])),
    ("env", collections.OrderedDict([
        ("frame", collections.OrderedDict([("x", 0), ("y", 0), ("w", 720), ("h", 1593)])),
        ("safeTop", {"h": 112, "name": "safe top"}),
        ("wxCapsule", {"x": 545, "y": 112, "w": 155, "h": 81}),
    ])),
    ("repo", "/some/repo"),
])
diff = {"env": {"frame": {"x": 0, "y": 104, "w": 720, "h": 1593, "align": "bottom"},
                "safeTop": {"h": 120}}}
out = serve._apply_diff_to_pack(raw, diff, {})
f = out["env"]["frame"]
assert f["align"] == "bottom", f
assert f["y"] == 104, f
assert out["env"]["safeTop"]["h"] == 120, out["env"]["safeTop"]
assert out["env"]["safeTop"]["name"] == "safe top", "name preserved"
assert out["env"]["wxCapsule"]["x"] == 545, "capsule untouched"
assert out["elements"]["hen"]["x"] == 255, "elements untouched"
assert out["repo"] == "/some/repo", "top-level untouched"
print("PASS")
```

- [ ] **Step 2: 运行验证它失败**

Run: `python3 /tmp/test_env_writeback.py`
Expected: FAIL — `AssertionError`（align 没写进去，因为 `_apply_diff_to_pack` 目前跳过非 elements 键）。

- [ ] **Step 3: 在 `_apply_diff_to_pack` 加 env 分支**

在 `serve.py` 的循环里，`if key in ("_added", "elasticZone", "anchorLine"): continue` 之前，加一个 env 专门处理（`env` 也要从通用元素处理中排除）。改法：把 env 键的 continue 合进那一行，并在函数末尾 `for pk in ("elasticZone","anchorLine")` 之后加 env 合并。

替换 `serve.py:220-222`：

```python
    for key, val in diff.items():
        if key in ("_added", "elasticZone", "anchorLine", "env"):
            continue
```

在 `serve.py:257-259`（`for pk in ("elasticZone", "anchorLine")` 那段）之后、`return raw` 之前插入：

```python
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
```

- [ ] **Step 4: 运行验证通过**

Run: `python3 /tmp/test_env_writeback.py`
Expected: `PASS`

- [ ] **Step 5: 语法检查 + 清理 + 提交**

```bash
cd /Users/bingwang/github/EyeToSpec
python3 -c "import ast;ast.parse(open('serve.py').read())" && echo OK
rm /tmp/test_env_writeback.py
git add serve.py
git commit -m "feat(serve): 写回支持 env.frame(x/y/w/h/align)+安全区,保留 wxCapsule/name"
```

---

### Task 2: app.js — align→y 推导 + anchorLine 兼容读取

**Files:**
- Modify: `web/app.js`（新增两个纯函数，靠近 `envFrameRect` at :488）

**Interfaces:**
- Consumes: `manifest.canvas.h`、`manifest.env.frame`、`manifest.anchorLine`、`anchorCy`（既有全局，:92）。
- Produces:
  - `anchorLineY(canvasH)` → number|null：锚线在画布上的 px 位置，兼容旧 cy(0–1) 与新 y(px)，也读实时 `anchorCy`。
  - `frameYForAlign(align, frameH, canvasH)` → number：按 align 推导 frame 顶边 y。

- [ ] **Step 1: 加两个纯函数**

在 `web/app.js` 的 `envFrameRect`（:488）之前插入：

```js
// Read the anchor line's canvas-px position, tolerant of both the legacy
// normalized cy (0..1) and a future px y. Live drag state (anchorCy) wins.
function anchorLineY(canvasH) {
  if (Number.isFinite(anchorCy)) return anchorCy * canvasH;   // live/seed fraction
  const a = manifest && manifest.anchorLine;
  if (a && Number.isFinite(a.y))  return a.y;                 // future: px
  if (a && Number.isFinite(a.cy)) return a.cy * canvasH;      // legacy: fraction
  return null;
}

// Derive the frame's top-edge y (px) from its align rule. baseline pins the
// frame's TOP edge to the anchor line; falls back to 0 if no line exists.
function frameYForAlign(align, frameH, canvasH) {
  switch (align) {
    case 'bottom':   return canvasH - frameH;
    case 'center':   return (canvasH - frameH) / 2;
    case 'baseline': { const ly = anchorLineY(canvasH); return ly == null ? 0 : ly; }
    case 'top':
    default:         return 0;
  }
}
```

- [ ] **Step 2: node --check**

Run: `cd /Users/bingwang/github/EyeToSpec && node --check web/app.js`
Expected: 无输出（通过）。

- [ ] **Step 3: 提交**

```bash
git add web/app.js
git commit -m "feat(app): frameYForAlign 推导 + anchorLineY 兼容(cy/px)"
```

---

### Task 3: app.js — env.frame 工作态 seed + drawEnv 用推导 y

**Files:**
- Modify: `web/app.js:172-196`（seed 区，加 frame 工作态）
- Modify: `web/app.js:488-500`（`envFrameRect` 用工作态 + 推导 y）

**Interfaces:**
- Consumes: `manifest.env.frame`、Task 2 的 `frameYForAlign`。
- Produces: 全局 `frameState`（`{x,y,w,h,align}` 或 null）；`envFrameRect()` 改读 `frameState`；新增 `recomputeFrameY()` 按 align 刷新 `frameState.y` 并 `sizeEnv()`。

- [ ] **Step 1: 加 frameState 全局 + seed**

在 `web/app.js` seed 区（`applyCanvasBackground();` at :183 之前）插入：

```js
  // Phone-frame working state (editable). null when the pack has no env.frame.
  // align is the source of truth; y is the baked px result of that rule.
  const _f = manifest.env && manifest.env.frame;
  frameState = _f ? {
    x: num(_f.x, null, 0),
    w: num(_f.w, null, 720),
    h: num(_f.h, null, (manifest.canvas && manifest.canvas.h) || 1280),
    align: _f.align || 'top',
    y: num(_f.y, null, 0),
  } : null;
  if (frameState) recomputeFrameY();   // bake y from align on load
```

在文件顶部全局声明区（`let anchorLineEl = null;` 附近，:93）加：

```js
let frameState = null;   // editable phone-frame state, or null if pack has none
```

- [ ] **Step 2: 加 recomputeFrameY**

在 `envFrameRect`（:488）之后插入：

```js
// Recompute frame.y from its align rule + current h/canvas, then reposition.
function recomputeFrameY() {
  if (!frameState) return;
  const canvasH = (manifest.canvas && manifest.canvas.h) || 1280;
  frameState.y = Math.round(frameYForAlign(frameState.align, frameState.h, canvasH));
  if (typeof sizeEnv === 'function') sizeEnv();
}
```

- [ ] **Step 3: envFrameRect 改读 frameState**

替换 `web/app.js:488-500` 的 `envFrameRect`：

```js
function envFrameRect() {
  const f = frameState;
  if (!f) return null;
  const S = dispScale();
  return {
    left: num(f.x, null, 0) * S,
    top:  num(f.y, null, 0) * S,
    w:    num(f.w, null, 720) * S,
    h:    num(f.h, null, 1280) * S,
    S,
  };
}
```

注意：`drawEnv`（:503）里 `if (!env || !env.frame) return;` 保持不变（首帧仍据 manifest 决定画不画）；`sizeEnv` 读 `envFrameRect()` 已自动走 frameState。

- [ ] **Step 4: node --check + 手验**

Run: `cd /Users/bingwang/github/EyeToSpec && node --check web/app.js`
Expected: 通过。
手验：重启 8793 后开 `editor.html?pack=absolute-td-live%2Fbattle`，frame 仍画在 y=104（bottom 未设时 align 默认 top → y=0；battle pack 现在 y=104 但 align 未设，故这步会看到 frame 跳到 y=0 —— 正常，Task 5 存 align=bottom 后恢复）。

- [ ] **Step 5: 提交**

```bash
git add web/app.js
git commit -m "feat(app): env.frame 工作态 seed + envFrameRect 读 frameState + recomputeFrameY"
```

---

### Task 4: editor.html + app.js — Phone frame 侧边栏面板

**Files:**
- Modify: `web/editor.html:48-56`（sidebar，加 frame section）
- Modify: `web/app.js`（新增 `renderFramePanel()` + 事件；在 `wireAlignBar()` 附近调用）
- Modify: `web/style.css`（面板样式）

**Interfaces:**
- Consumes: `frameState`、`recomputeFrameY`、`manifest.canvas.h`、`manifest.env`。
- Produces: `renderFramePanel()` 渲染两态（有/无 frame）；`addFrame()` / `removeFrame()`；align/w/h/x/safe 输入的 change handler；改动后 `recomputeFrameY()` + 重渲面板。全局 `frameDirty=true` 标记供 buildOutput 用。

- [ ] **Step 1: html 加常驻 section**

在 `web/editor.html` 的 `<ul id="element-list">` 那个 sidebar-section（:49-55）之后插入：

```html
      <div class="sidebar-section frame-section">
        <h2>Phone frame</h2>
        <div id="frame-panel" class="frame-panel"></div>
      </div>
```

- [ ] **Step 2: app.js 加 renderFramePanel + 操作**

在 `web/app.js` 靠近 `wireAlignBar`（:1248 区）之后插入：

```js
let frameDirty = false;   // frame panel touched -> buildOutput emits env

const FRAME_ALIGNS = ['top', 'bottom', 'center', 'baseline'];

function renderFramePanel() {
  const panel = document.getElementById('frame-panel');
  if (!panel) return;
  if (!frameState) {
    panel.innerHTML =
      '<div class="frame-empty">No phone frame on this pack.</div>' +
      '<button id="frame-add" class="btn btn-ghost">+ Add phone frame</button>';
    panel.querySelector('#frame-add').addEventListener('click', addFrame);
    return;
  }
  const f = frameState;
  const st = (manifest.env && manifest.env.safeTop && manifest.env.safeTop.h) || 0;
  const sb = (manifest.env && manifest.env.safeBottom && manifest.env.safeBottom.h) || 0;
  panel.innerHTML =
    '<div class="frame-row"><label>w</label><input data-fk="w" type="number" value="' + f.w + '"></div>' +
    '<div class="frame-row"><label>h</label><input data-fk="h" type="number" value="' + f.h + '"></div>' +
    '<div class="frame-row"><label>x</label><input data-fk="x" type="number" value="' + f.x + '"></div>' +
    '<div class="frame-row"><label>align</label><div class="frame-aligns">' +
      FRAME_ALIGNS.map(a =>
        '<button class="align-opt' + (f.align === a ? ' on' : '') + '" data-align-opt="' + a + '">' + a + '</button>'
      ).join('') +
    '</div></div>' +
    '<div class="frame-row"><label>y (derived)</label><span class="frame-y">' + f.y + ' px</span></div>' +
    '<div class="frame-row"><label>safe top</label><input data-sk="safeTop" type="number" value="' + st + '"></div>' +
    '<div class="frame-row"><label>safe bottom</label><input data-sk="safeBottom" type="number" value="' + sb + '"></div>' +
    '<button id="frame-remove" class="btn btn-ghost">Remove frame</button>';

  panel.querySelectorAll('[data-fk]').forEach(inp =>
    inp.addEventListener('change', () => {
      const k = inp.dataset.fk, v = parseFloat(inp.value);
      if (Number.isFinite(v)) { frameState[k] = v; frameDirty = true; recomputeFrameY(); renderFramePanel(); }
    }));
  panel.querySelectorAll('[data-align-opt]').forEach(btn =>
    btn.addEventListener('click', () => {
      frameState.align = btn.dataset.alignOpt; frameDirty = true; recomputeFrameY(); renderFramePanel();
    }));
  panel.querySelectorAll('[data-sk]').forEach(inp =>
    inp.addEventListener('change', () => {
      const band = inp.dataset.sk, v = parseFloat(inp.value);
      if (!Number.isFinite(v)) return;
      if (!manifest.env) manifest.env = {};
      if (!manifest.env[band]) manifest.env[band] = {};
      manifest.env[band].h = v; frameDirty = true;
      drawSafeBands(); sizeEnv();
    }));
  const rm = panel.querySelector('#frame-remove');
  if (rm) rm.addEventListener('click', removeFrame);
}

function addFrame() {
  const canvasH = (manifest.canvas && manifest.canvas.h) || 1280;
  const canvasW = (manifest.canvas && manifest.canvas.w) || 720;
  frameState = { x: 0, w: canvasW, h: canvasH, align: 'top', y: 0 };
  frameDirty = true;
  if (!manifest.env) manifest.env = {};
  manifest.env.frame = { x: 0, y: 0, w: canvasW, h: canvasH, align: 'top' };
  drawEnv(); recomputeFrameY(); renderFramePanel();
}

function removeFrame() {
  frameState = null;
  frameDirty = true;
  if (manifest.env) delete manifest.env.frame;
  if (envFrameEl) { envFrameEl.remove(); envFrameEl = null; }
  sizeEnv(); renderFramePanel();
}
```

- [ ] **Step 3: 初始化调用**

在 seed 区末尾 `wireAlignBar();`（:196）之后加：

```js
  renderFramePanel();
```

- [ ] **Step 4: css**

在 `web/style.css` 末尾加：

```css
.frame-panel { display: flex; flex-direction: column; gap: 6px; }
.frame-row { display: flex; align-items: center; gap: 8px; }
.frame-row label { width: 92px; font-size: 12px; opacity: .8; }
.frame-row input { width: 80px; }
.frame-aligns { display: flex; gap: 4px; flex-wrap: wrap; }
.align-opt { padding: 2px 8px; font-size: 12px; border: 1px solid #555; background: transparent; color: inherit; border-radius: 4px; cursor: pointer; }
.align-opt.on { background: #e0a94f; color: #222; border-color: #e0a94f; }
.frame-y { font-size: 12px; opacity: .7; }
.frame-empty { font-size: 12px; opacity: .6; margin-bottom: 6px; }
```

- [ ] **Step 5: node --check + 手验**

Run: `cd /Users/bingwang/github/EyeToSpec && node --check web/app.js`
手验（重启 8793）：battle 页面板出现 w/h/x/align/y。点 align=bottom → frame 滑到 y=1697−1593=104，y(derived) 显示 104；点 top → 0；center → 52；baseline → frame 顶边贴锚线（battle 无锚线则回 0）。开一个无 env.frame 的 pack → 面板显示 "Add phone frame"。

- [ ] **Step 6: 提交**

```bash
git add web/editor.html web/app.js web/style.css
git commit -m "feat(app): Phone frame 侧边常驻面板(w/h/x/align + 安全区 + Add/Remove)"
```

---

### Task 5: app.js — buildOutput 导出 env

**Files:**
- Modify: `web/app.js:1596-1656`（`buildOutput`，`return out` 之前加 env）

**Interfaces:**
- Consumes: `frameState`、`frameDirty`、`manifest.env`。
- Produces: `frameDirty` 为真时，`out.env = {frame:{x,y,w,h,align}, [safeTop:{h}], [safeBottom:{h}]}`；frame 被删则 `out.env` 不含 frame（serve 不合并 → 但删除需显式处理，见 Step 说明）。

- [ ] **Step 1: buildOutput 加 env 段**

在 `web/app.js` `buildOutput` 的 `return out;`（:1656）之前插入：

```js
  // env.frame + safe bands: only when the frame panel was touched this session.
  if (frameDirty) {
    const env = {};
    if (frameState) {
      env.frame = {
        x: Math.round(frameState.x), y: Math.round(frameState.y),
        w: Math.round(frameState.w), h: Math.round(frameState.h),
        align: frameState.align || 'top',
      };
    }
    const et = manifest.env && manifest.env.safeTop;
    const eb = manifest.env && manifest.env.safeBottom;
    if (et && Number.isFinite(et.h)) env.safeTop = { h: Math.round(et.h) };
    if (eb && Number.isFinite(eb.h)) env.safeBottom = { h: Math.round(eb.h) };
    if (Object.keys(env).length) out.env = env;
  }
```

- [ ] **Step 2: node --check**

Run: `cd /Users/bingwang/github/EyeToSpec && node --check web/app.js`
Expected: 通过。

- [ ] **Step 3: 端到端手验**

重启 8793 → battle 页 → 面板选 align=bottom → 💾 Save to pack → 打开 `config/absolute-td-live/battle/pack.json`，确认 `env.frame` 有 `"align": "bottom"`、`"y": 104`，`wxCapsule`/`elements`/`repo`/`runtime` 原封不动。

- [ ] **Step 4: 提交**

```bash
git add web/app.js
git commit -m "feat(app): buildOutput 导出 env.frame + 安全区(frameDirty 门控)"
```

---

## 说明:frame 删除的写回

本 plan Task 1 的 serve 合并逻辑是"只加不删"：`removeFrame()` 后 `out.env` 不含 `frame`，serve 不会主动删原 pack 的 `env.frame`。这是**有意的保守设计**——删除 frame 属低频操作，且误删风险高。若确需支持写回删除，作为 out-of-scope 后续：在 diff 里发 `env.frame = null` 信号，serve 端 `if "frame" in env_diff and env_diff["frame"] is None: env.pop("frame", None)`。本 plan 不做，面板 Remove 只改运行态（不 Save 就不落盘）。

---

## Self-Review

- **Spec 覆盖:** §2 align/y 正交 → Task 2/3；§3 契约 → Task 1(写回)+Task 3(seed);§4 anchorLine 兼容 → Task 2 `anchorLineY`（px 收口本身 out-of-scope,spec §8）;§5 面板两态 → Task 4;§6 写回 → Task 1+Task 5;§7 验证 → 各 Task 手验步骤。baseline=顶边 → `frameYForAlign` case 'baseline'。
- **占位符:** 无 TBD/TODO;每个改码步骤都有完整代码。
- **类型一致:** `frameState{x,y,w,h,align}` 贯穿 Task 3/4/5;`frameYForAlign(align,frameH,canvasH)`、`anchorLineY(canvasH)` 在 Task 2 定义、Task 3 消费,签名一致;`frameDirty` Task 4 定义、Task 5 消费;serve `env.frame` 键 `x/y/w/h/align` 与前端导出一致。
- **已知取舍:** frame 删除写回不做(见上节);anchorLine px 收口拆下一个 plan。

## Execution Handoff

见下方对话。

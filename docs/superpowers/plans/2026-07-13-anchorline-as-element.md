# anchorLine 收敛为 line 元素 — 实现 plan

> 依据 spec: `docs/superpowers/specs/2026-07-13-anchorline-as-element-design.md`。只做 Config 契约 + EyeToSpec 支持,不碰下游游戏端/迁移/单测。

**Goal:** 基准线从"凌驾契约的特殊机制"降为普通 `type:"line"` 元素(px + 左上角,key 即标识),EyeToSpec 能渲染/拖/存它;删掉整套 anchorLine 旧机制;frame baseline 改读 line 元素 y。

**Tech:** vanilla JS(web/app.js)+ Python http.server(serve.py)。验证 = `node --check` / `ast.parse` + ad-hoc roundtrip。

---

### Task 1: serve.py 注册 line 类型

- `_DETAIL_FIELDS`(:124)加 `"line": ()`。这样 `_flatten_element` 的 `etype not in _DETAIL_FIELDS` 判断不再把 line 降级为 box,`type:"line"` 得以透传到前端节点。
- 无其它改动:x/y/w/h 已在 `_COMMON_FIELDS`/`_GEO_KEYS`,flatten/rebuild/writeback 全走通用路径。
- 验证:roundtrip 脚本 —— 构造带 `{"type":"line","x":0,"y":763,"w":720,"h":4,"detail":{}}` 的 pack → `_flatten_element` 出 `type=="line"` 且含 x/y/w/h;`_apply_diff_to_pack` 改 y=800 → 顶层 y 更新、detail 不污染。
- `python3 -c "import ast;ast.parse(open('serve.py').read())"`。

### Task 2: app.js 加 line 渲染

- `renderElements`(:815 dispatch)在 `if (el.file)` 之前加前置分支 `if (el.type === 'line')`:创建横线 div(`node.classList.add('el-line')`,背景色填充,高度=h 由 placeNode 处理),附通用 handle/rot、走通用 pointerdown 拖拽/resize。inspector 复用通用 x/y/w/h。
- CSS 加 `.el-line { background: #4a9eff; }`(醒目蓝,复用旧 anchor 观感)。
- `node --check`。

### Task 3: app.js 清掉旧 anchorLine 机制

- 删:`LINE_KIND`(:41)、`ELASTIC_MIN_H`(:44)、`anchorCy`(:92)、`anchorLineEl`(:93)、`anchorDrag` 相关。
- 删:`drawAnchorLine`(:727)、`onAnchorDown/Move/Up`(:777-800)、seed `anchorCy=`(:176)、init `drawAnchorLine()`(:200)。
- 删:`buildOutput` 的 `anchorLine`/`elasticZone` 导出段(:1764-1772)。
- 顶部注释(:36-44、:89-93)清理。
- `guideCy`/guideLine(:97,:182,:758-773)**不动**。
- `node --check`。

### Task 4: app.js 改 anchorLineY 读 line 元素

- `anchorLineY(canvasH)`(:501)改为:遍历工作态 elements,找 `type==='line'`(优先 key `anchor-line`)返回其 `y`(px);无则 null。删掉 anchorCy / manifest.anchorLine.cy 兼容分支。
- frame baseline 自动受益(frameYForAlign 'baseline' 已调此函数)。
- `node --check` + 手验:含 line 元素的 pack,frame 选 baseline → 顶边贴 line y。

### Task 5: 契约文档 + 提交

- `docs/absolute-contract.md` 元素类型区补 `line`(见 spec §4)。
- 提交:serve.py / web/app.js / web/style.css / docs。

---

## Out-of-scope
公共转换库、游戏端 reader、旧 json 迁移、单测、vline、guideLine px 化 —— 均下游/后续。

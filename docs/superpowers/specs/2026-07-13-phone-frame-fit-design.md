# Phone Frame Fit — 设计 spec

日期: 2026-07-13
状态: 待 review

## 1. 背景与问题

EyeToSpec 的画布(canvas)= 背景图的真实像素尺寸;**phone frame** = 手机屏幕视窗在画布上的一个矩形(`env.frame`,px + 左上角)。背景图常比手机屏高(如 battle:背景 720×1697,frame 720×1593),多出来的部分是"给顶部留白"的设计意图。

**现状痛点:**

1. frame 的竖直位置(`env.frame.y`)只能手算硬填(如 `1697−1593=104`)。没有"贴顶/贴底/居中/贴线"的声明式表达,改画布高就得重算。
2. frame 是**只读**的:从 `manifest.env.frame` 画出来,不可选、不可编辑,`buildOutput()` 从不导出 `env`。写回时 frame 只是因为"没被 diff 碰"而被 `_apply_diff_to_pack` 原样保留。
3. owner 需要一个"运行时屏高适配规范"字段:真机屏幕高矮不一,代码要知道"这一屏往哪个边收敛"。

## 2. 核心概念:align 与 y 是正交的两件事

这是本设计的地基,先厘清,避免把两个字段误当成"同一个值的两种存法"(那样才会打架):

- **`align`** = **运行时屏高适配规范**。真机屏高千变万化,游戏代码靠它决定"这一屏往哪个边贴"。取值 `top` / `bottom` / `center` / `baseline`。这是给游戏运行时用的语义。
- **`y`** = **编辑期标定的初始屏位置**(frame 顶边在画布上的 px)。它回答"在这块设计基准画布上,frame 摆在哪"。

两者**都存,不打架**,因为回答的是不同问题:`y` 说"设计基准屏 frame 在画布哪",`align` 说"真机屏高变了往哪个边收敛"。这与元素同时有 `x/y`(位置)和 `anchor`(适配锚点)是完全同构的双字段结构。

> 反例澄清:元素的 `anchor` 那次之所以要"只存一份",是因为当时是同一个位置值存了两份(几何对齐结果 vs anchor)。这里 `y`(初始位置)和 `align`(适配规则)是两个独立的量,不是同一个值。

> y 的真源性说明:编辑期 y 由 align 推导(见下表),面板里只读展示;但它作为**具体 px 值持久化写回**,让游戏运行时能直接读到"设计基准屏的初始位置",无需自己跑 align 公式。即 align 是规则、y 是该规则在基准画布上烘焙出的结果,二者一起导出。编辑期单一真源是 align,y 是它的固化产物。

### align 各取值 → frame.y 的推导(编辑期实时预览用)

设 `canvasH = canvas.height`,`h = frame.h`:

| align | frame.y(实时滑到) |
|---|---|
| `top` | `0` |
| `bottom` | `canvasH − h` |
| `center` | `(canvasH − h) / 2` |
| `baseline` | `anchorLine 的 y`(见 §4) |

**交互语义:** 在面板里改 `align` 或 `h` 或 `canvasH` 时,frame 在画布上**实时滑到** align 对应的位置,并把算出的值写进 `y`(所见即所存)。`baseline` 模式下,frame 顶边跟着 anchorLine 走。owner 选定 baseline 锚点 = **frame 顶边**(与"左上角定位"心智一致:frame 的 y 就是它顶边)。

## 3. env.frame 契约(新增 / 明确)

```jsonc
"env": {
  "frame": {
    "x": 0,            // px, frame 左上角横向位置(保留,横向不做 align)
    "y": 104,          // px, frame 顶边竖直位置 = 编辑期标定的初始屏位置
    "w": 720,          // px, 直接编;长宽比 = w/h,不单存
    "h": 1593,         // px, 直接编
    "align": "bottom"  // 新增: top|bottom|center|baseline, 运行时屏高适配规范
  },
  "safeTop":    { "h": 112, "name": "safe top 7%" },   // px(既有,沿用)
  "safeBottom": { "h": 64,  "name": "safe bottom 4%" },
  "wxCapsule":  { ... }                                 // 既有,不改
}
```

- `align` 缺省 = `top`(读取时 `align || 'top'`)。
- `y` 始终写(初始位置的真源);加载时若 `align` 非空,面板按 align 重算并预览,但 `y` 本身作为标定结果持久化。
- `env.frame` 整体**可选**:无 frame → 面板 disabled(见 §5)。

## 4. anchorLine 契约收口:cy(0–1) → y(px)

### 4.1 动机

项目铁律:**config/EyeToSpec 这层一律 px + 左上角;归一化/换算是游戏运行时公共库的事,不在 config 这步。** anchorLine 目前导出 `anchorLine.cy`(0–1 fraction)、`elasticZone.topCy`(0–1),是唯一没跟上 px 收口的遗留。

### 4.2 目标契约

- EyeToSpec 导出 **px**:`anchorLine.y`(px)取代 `anchorLine.cy`;divider 模式 `elasticZone.topY`(px)取代 `topCy`。
- baseline 模式:`frame.y = anchorLine.y` 直接相等,零换算。
- 游戏运行时公共库负责换回 fraction(reader 读到 px 后 `/ canvasH`)。

### 4.3 跨仓影响(⚠️ 范围大于 frame-fit 主体)

现存消费方(经核查):

- **游戏运行时** `apps/wechat-minigame/game.js`(构建产物)+ 源 `apps/web-client/src/ui/layout/base/page-layout.ts`(`resolveElasticZone`,读 `topCy` 0–1)、anchorLine reader。
- **5 个冻结的 ground-truth 布局 json**(shop / challenge / henhouse / elite-select / battle-field),内含 `topCy` / `anchorLine.cy` 的 0–1 值。
- **多个单测** 断言 `typeof meta.elasticZone.topCy === "number"` 且值域 0–1。

因此 cy→px 是**跨两个仓库**的迁移,且触碰 SKIP 里故意冻结的页。

### 4.4 分期策略(本 spec 的关键决策点)

**frame-fit 主体不依赖 anchorLine 立即改 px。** baseline 加载用一个**兼容边界函数**读锚线,px 迁移前后都不崩:

```js
// 读锚线的画布 px 位置,兼容旧 cy(0–1) 与新 y(px)
function anchorLineY(canvasH) {
  if (Number.isFinite(manifest.anchorLine?.y))  return manifest.anchorLine.y;        // 新: px
  if (Number.isFinite(manifest.anchorLine?.cy)) return manifest.anchorLine.cy * canvasH; // 旧: fraction
  return null;
}
```

anchorLine 的 px 收口(EyeToSpec 导出改 px + 游戏端 reader 加 px→fraction + 5 个 json 迁移 + 单测更新)拆成**紧随其后的第二个 plan**,由 owner review 时定"这次一起做还是下一轮"。frame-fit 主体先落地。

## 5. Phone frame 侧边栏面板

### 5.1 入口:侧边栏常驻 section

frame 是全局唯一对象(一个 pack 一个),不做画布点选/拖拽 —— **只在侧边常驻 "Phone frame" 面板里调**。理由:frame 是全局基准,标定一次即固定,不需要画布手感,反而避免误拖;不为"唯一对象"付"多对象选择"的复杂度。位置:侧边栏 Elements 列表附近,单独一块 section。

### 5.2 两态

- **无 `env.frame`**:面板 disabled(输入框灰掉不可编辑),顶部一个 **"+ Add phone frame"** 按钮。点击用默认值创建(`w=720, h=canvasH, x=0, y=0, align='top'`),面板激活。
- **有 `env.frame`**:面板可编辑,并提供 **"Remove frame"** 回到 disabled 态。

### 5.3 字段(可编辑项)

| 控件 | 绑定 | 说明 |
|---|---|---|
| w | `frame.w` (px) | 直接编 |
| h | `frame.h` (px) | 直接编,长宽比 = w/h 自然得出 |
| x | `frame.x` (px) | 横向位置 |
| align | `frame.align` | 四选一按钮 top/bottom/center/baseline;选后 frame 实时滑动并更新 y |
| safe top | `safeTop.h` (px) | 上安全区阈值 |
| safe bottom | `safeBottom.h` (px) | 下安全区阈值 |

y **只读显示**(不给输入框):它是 align + h + canvasH 推导出的初始位置,展示当前算出的 px 值即可;要改位置就改 align,不手填 y(手填会和 align 预览打架)。

## 6. 写回(buildOutput → serve)

`buildOutput()` 目前不导出 `env`。新增:frame 面板有改动时,输出携带 `env.frame`(x/y/w/h/align)与变更的 `env.safeTop.h`/`env.safeBottom.h`。

serve 写回:`_apply_diff_to_pack` 需支持把 diff 里的 `env`(尤其 `env.frame.*`、`env.safeTop.h`、`env.safeBottom.h`)合并进原 pack 的 `env`(既有元素/顶层字段的保留逻辑不变)。`align` 空串 → 视为默认 top,可 pop。

## 7. 验证

- `python3 -c "import ast;ast.parse(open('serve.py').read())"` + `node --check web/app.js`。
- battle pack:面板选 `bottom` → frame 滑到 y=104(=1697−1593),Save → pack.json 的 `env.frame` 有 `align:"bottom"` 且 `y:104`,repo/runtime/elements 原封不动。
- 选 `top` → y=0;`center` → y=52;`baseline` → frame 顶边贴 anchorLine(用兼容函数,cy 或 y 都能读)。
- 无 env.frame 的 pack:面板 disabled,点 "Add phone frame" 创建默认 frame,面板激活;"Remove" 回到 disabled 且写回删掉 env.frame。
- 阴性:不动面板直接 Save,env 原样保留(不出现在 diff)。

## 8. Out-of-scope(后续 plan)

- anchorLine `cy`→`y` px 收口(§4.3/4.4):游戏端 reader 加 px→fraction、5 个冻结 json 迁移、单测更新。跨仓,单独 plan。
- frame 横向 align(目前只做竖直)。

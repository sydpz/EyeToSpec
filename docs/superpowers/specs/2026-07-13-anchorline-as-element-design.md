# anchorLine 收敛为普通 line 元素 — 设计 spec

日期: 2026-07-13
状态: 待 review

## 0. 出发点:三层架构中的定位

本 spec 只做**最上层(Config)**,不碰下游。整条链路:

1. **Config(EyeToSpec 产出)** —— 纯粹、干净的坐标契约。px + 左上角,一切皆元素。只如实描述"设计稿上有什么、在哪",**不含业务语义、不含运行时换算**。EyeToSpec 是产出并编辑这层 config 的工具。
2. **公共转换库(将来开发)** —— 吃 config,吐业务要的数据结构。所有归一化 / px→fraction / 屏幕适配 / "把 line 元素解释成滚动区顶边"之类语义都收敛在这层。
3. **业务层(游戏页面)** —— 拿转换库的结构构建页面,不直接碰 config、不自己换算。

**本 spec 的唯一任务**:把 config 里"基准线"这个东西设计干净 + 让 EyeToSpec 支持它。**不用考虑历史包袱** —— 游戏端现在怎么读 `elasticZone.topCy`、旧页面怎么存 0–1、单测断言什么,都是待重构的下游,不是本 spec 要迁就或保护的对象。它们将来通过转换库适配新契约。

## 1. 问题

EyeToSpec 现在的"基准线"不是元素,而是一套凌驾于契约之上的特殊机制:

- 顶层导出 `anchorLine.cy`(0–1)或 `elasticZone.topCy`(0–1),由 URL `?line=divider|bgAnchor`(`LINE_KIND`)切换。
- 独立全局态 `anchorCy`、独立渲染 `drawAnchorLine()`、独立拖拽 `onAnchorDown/Move/Up`。
- 值是 0–1 归一化,违反"config 一律 px + 左上角";且承载了 `topCy`/`elasticZone` 这类**业务语义**,超出 config 层职责。

这是历史遗留:让工具背了本属于转换库/业务层的知识。

## 2. 目标契约:基准线 = 普通 line 元素

```jsonc
{
  "elements": {
    "anchor-line": {          // key 即标识:下游按约定 key 认出它
      "type": "line",
      "x": 0,
      "y": 763,               // px, 左上角原点 —— 竖直位置
      "w": 720,
      "h": 4,                 // 线宽(渲染成细高矩形)
      "depth": 99,
      "detail": {}
    }
  }
}
```

- **key 即标识**:不新增任何 name 字段。下游约定读某个 key(如 `anchor-line`)。owner 定案:"元素的 key 就是标识"。
- **type: "line"**:新增元素类型。语义上一条水平线只有 `y` 有意义,但**结构上复用通用 `x/y/w/h`**(x=0, w=画布宽, h=线宽),使 serve flatten / `_GEO_KEYS` / `_apply_diff_to_pack` / app.js 渲染全部零特例 —— line 就是一个扁平 box,渲染成横线。
- **px + 左上角**:`y` 即位置,cy 遗留消失。
- **命名预留**:type 用 `"line"`(泛指线/矩形线)。未来若要竖线,另起 type(如 `"vline"`),不与本类型抢名。type 归 type(line)、key 归 key(anchor-line),各司其职。

**顶层 `anchorLine` / `elasticZone` 从 EyeToSpec 彻底消失。** 这两个概念不再属于 config 层。

## 3. EyeToSpec 改动

### 3.1 serve.py:注册 line 类型

- `_DETAIL_FIELDS`(:124)加 `"line": ()`(无 detail 字段,纯几何)。
- 其余 flatten/rebuild/writeback 走通用路径,无需特例(line 的 x/y/w/h 已在 `_COMMON_FIELDS` / `_GEO_KEYS`)。

### 3.2 app.js:line 元素渲染 + 清掉旧机制

**新增 line 渲染**(`renderElements` :856 的 else 分支旁,加 `type==='line'` 前置分支):一条横线 div(用背景色 + 高度=h)。inspector 正常显示 x/y/w/h,走通用拖拽/resize。

**删除整套 anchorLine 特殊机制**:
- 删 `LINE_KIND`(:41)、`ELASTIC_MIN_H`(:44)、`anchorCy`(:92)、`anchorLineEl`(:93)。
- 删 `drawAnchorLine`(:727)、`onAnchorDown/Move/Up`(:777-800)、seed 里 `anchorCy = ...`(:176)、init 里 `drawAnchorLine()`(:200)。
- 删 `buildOutput` 的 `anchorLine`/`elasticZone` 导出段(:1767-1772)。
- CSS `.anchor-line` / `.anchor-label` / `.anchor-grip` 清理(可留,无害)。

**改 `anchorLineY(canvasH)`**(frame Task-2 加的兼容函数,:501):改为在 `elements` 里找 `type==='line'` 的元素(优先约定 key `anchor-line`),返回其 `y`(px)。**frame 的 baseline 对齐自动受益** —— 读的就是 line 元素的 px y,不再依赖 anchorCy。

### 3.3 guideLine 不动

`guideCy`(:97)是 serve 派生的只读 overlay 参考线(非用户拖拽的基准线),本 spec **不碰**,留作后续。

## 4. 契约文档

`docs/absolute-contract.md` 元素类型区补 `line`:
- `type: "line"` = 水平基准线/分隔线;`x/y/w/h` px(x=0、w=画布宽、h=线宽为惯例);无 detail 字段。
- 用途注记:EyeToSpec 只存位置;线代表什么(滚动区顶边 / 背景对齐 / …)由**转换库 + 业务层**按约定 key 决定,config 不含该语义。

## 5. 验证

- `python3 -c "import ast;ast.parse(open('serve.py').read())"` + `node --check web/app.js`。
- serve roundtrip:构造带 `type:"line"` 元素的 pack → flatten → 前端节点含 x/y/w/h;写回 diff 改 line 的 y → 顶层 y 更新、detail 不污染。
- EyeToSpec 手验:开一个含 line 元素的 pack → 渲染成横线、能拖(y 变)、inspector 显示 px y;Save → `elements.<key>.y` 是 px;pack.json 无顶层 `anchorLine`/`elasticZone`。
- frame baseline:pack 有 line 元素时,frame 面板选 baseline → frame 顶边贴该 line 的 y。

## 6. Out-of-scope(下游 / 将来)

- 公共转换库(config → 业务结构)。
- 游戏端 reader、旧页面 json 迁移、单测 —— 下游接转换库时各自适配,不在本 spec。
- 竖线(vline)类型 —— 仅预留 type 名。
- guideLine 的元素化 / px 化。

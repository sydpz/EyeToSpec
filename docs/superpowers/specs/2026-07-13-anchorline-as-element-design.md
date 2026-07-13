# anchorLine 降格为普通 line 元素 — 设计 spec

日期: 2026-07-13
状态: 待 review

## 1. 背景与问题

EyeToSpec 现在有一条可拖动的水平"基准线"(baseline / divider),它**不是**普通元素,而是一套特殊机制:

- 顶层字段导出:`anchorLine.cy`(0–1 fraction)或 `elasticZone.topCy`(0–1),由 URL 参数 `?line=divider|bgAnchor`(`LINE_KIND`)切换。
- 独立的全局状态 `anchorCy`、独立渲染 `drawAnchorLine()`、独立拖拽 `onAnchorDown/Move/Up`。
- 值是 **0–1 归一化**,违反项目铁律"config/EyeToSpec 这层一律 px + 左上角"。

**owner 的设计取向(本 spec 的依据):**

> 这条基准线就是 `elements` 里的一个普通元素,自然有自己的 px 坐标。它为什么需要、代表什么,是**游戏业务自己决定**的 —— EyeToSpec 只管让它被拖到对的位置、存下 px 坐标。游戏运行时按**约定的元素 key** 找到它,读 px,自己换算、自己决定语义。EyeToSpec 不需要懂 elasticZone / topCy / LINE_KIND 这些业务概念。

## 2. 目标契约

### 2.1 基准线 = 普通 line 元素

```jsonc
{
  "elements": {
    "anchor-line": {           // key 即标识:游戏按约定 key 认出它
      "type": "line",
      "x": 0,
      "y": 763,                // px, 左上角原点 —— 这就是原来的 cy×canvasH
      "w": 720,
      "h": 4,
      "depth": 99,
      "detail": {}
    }
  }
}
```

- **key 即 name**:不新增任何标识字段。游戏运行时约定读某个 key(如 `anchor-line`)。owner 定案:"元素的 key 就是标识"。
- **type: "line"**:新增元素类型。一条 line 语义上只有 `y` 有意义,但**结构上复用通用 `x/y/w/h`**(x=0, w=画布宽, h=线宽如 4),这样 serve flatten / `_GEO_KEYS` / `_apply_diff_to_pack` / app.js 渲染全部零特例 —— line 就是一个扁平的 box。渲染成一条横线(细高矩形)。
- **px + 左上角**:`y` 就是线的竖直位置,cy 遗留彻底消失。

> 命名预留:type 用 `"line"`(通用水平/矩形线)。若未来要竖线,另起 `type`(如 `"vline"`),不与本类型抢名。owner 讨论中提到过 `anchor-line` 作 type,但结论是 **type 归 type(line)、key 归 key(anchor-line)** 各司其职,不重复。

### 2.2 顶层 anchorLine / elasticZone 字段废除(EyeToSpec 侧)

EyeToSpec 不再导出 `anchorLine` / `elasticZone`。这两个概念从工具消失。

## 3. 职责边界

| 关注点 | 归属 |
|---|---|
| 线画在哪(px 坐标) | EyeToSpec(拖动 line 元素) |
| 线代表什么(滚动区顶边?背景对齐?) | 游戏业务 |
| topCy(divider 位置)| 由 line 元素的 `y ÷ canvasH` 换算,游戏 reader 做 |
| minGap / maxGap(弹性拉伸行为)| **纯游戏业务**,EyeToSpec 从不碰,游戏自己在 layout json 里 author |

关键区分:`elasticZone` 现在包含 `{ topCy, minGap, maxGap }`。其中 **只有 topCy 是"线的位置"**(EyeToSpec 能给);`minGap`/`maxGap` 是拉伸行为参数,游戏自己写。所以迁移后:游戏从 line 元素算出 topCy,`minGap`/`maxGap` 继续留在 layout json 顶层由游戏 author。

## 4. 游戏运行时 reader 改动(absolute-td 仓)

`apps/web-client/src/ui/layout/base/page-layout.ts` 的 `resolveElasticZone`:

- **现状**:读 `raw.elasticZone.topCy`(0–1)。
- **改为**:先在 `raw.elements` 里按约定 key(`anchor-line`)找 line 元素;若找到,`topCy = element.y / canvasH`;`minGap`/`maxGap` 仍读 `raw.elasticZone`(或新位置)。若没找到,回退 `fallback.topCy`。
- `bgAnchor` 模式(`anchorLine.cy`)同理:游戏若有消费点,改从 line 元素 key 读 px→fraction。

> ⚠️ 这一步跨仓,且触碰 shop / challenge / henhouse / battle-field 四个**冻结的 ground-truth** 布局 json + 相关单测(断言 `typeof elasticZone.topCy === "number"`)。

## 5. 迁移

现存 0–1 值 → line 元素(px):

| 页面 | 现值 | 迁移 |
|---|---|---|
| shop | `elasticZone.topCy: 0.164` | + `elements.anchor-line{type:line, y: round(0.164×canvasH)}`,删 topCy |
| challenge | `topCy: 0.45`(fallback) | 同上 |
| henhouse | 有 topCy | 同上 |
| battle-field | 有 anchorLine/elasticZone | 同上 |
| (EyeToSpec) loadout / td-home pack | 有 anchorLine | 同上 |

`minGap`/`maxGap` 保留在原处不动。

## 6. EyeToSpec 代码清理(app.js)

删除整套 anchorLine 特殊机制,基准线走普通元素路径:

- 删 `LINE_KIND`(:41)、`ELASTIC_MIN_H`(:44)、`anchorCy`(:92)、`anchorLineEl`(:93)。
- 删 `drawAnchorLine`(:727)、`onAnchorDown/Move/Up`(:777-)、seed 里的 `anchorCy = ...`(:176)。
- 删 `buildOutput` 的 `anchorLine`/`elasticZone` 导出段(:1767-1772)。
- `anchorLineY(canvasH)`(Task-2 frame 加的兼容函数,:501):改为在 elements 里找约定 key 的 line 元素读 `y`(px);保留函数名给 frame 的 baseline 用 —— **frame baseline 对齐自动受益**,读的就是 line 元素的 px y。
- 新增 `type:"line"` 的渲染:一条横线(细高矩形 div)。inspector 正常显示 x/y/w/h;拖拽走通用元素拖拽(可只锁竖直,渲染细节)。
- `guideCy`(:97,只读 overlay 参考线)本 spec **不动**(它是 serve 派生的只读线,非用户拖拽的基准线),留作后续。

## 7. 验证

- `python3 -c "import ast;ast.parse(open('serve.py').read())"` + `node --check web/app.js`。
- EyeToSpec:开一个带 line 元素的 pack,line 渲染成横线、能拖、inspector 显示 px y;Save → pack.json 的 `elements.anchor-line.y` 是 px,无顶层 `anchorLine`/`elasticZone`。
- 游戏端:`resolveElasticZone` 从 line 元素算 topCy,单测改断言;shop/henhouse 等页面滚动区顶边位置与迁移前一致(px÷canvasH ≈ 原 topCy)。
- frame baseline:选 baseline → frame 顶边贴 line 元素的 y。

## 8. 风险与分期

- **跨两仓 + 碰冻结页**:这是本 spec 最大风险。建议实现时先在 EyeToSpec 侧完成(line 类型 + 迁移工具),再改游戏端 reader,最后一次性迁 4 个 json + 改单测,每步独立可验。
- **guideLine / guideCy** 不在本 spec。
- **fitMode / minGap / maxGap** 语义不变,只是不再和 topCy 绑在一个字段里(topCy 来源改为 line 元素)。

## 9. Out-of-scope

- 竖线(vline)类型 —— 预留 type 名,不实现。
- guideLine 的 px 化。

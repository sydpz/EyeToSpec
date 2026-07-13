# Canvas / Frame / Background —— 坐标怎么算(别再搞反)

> 这份文档钉死一件反复搞错的事:**canvas、frame、background 三者的坐标关系**。
> owner 2026-07-13 定案。写它的直接原因:助手在 endless 页把"谁比谁大、y 带不带
> 负号、+682 还是 −682"连环搞反 —— 根子是**参照物没摆对**。把心智模型固定下来,
> 下次照着算,不靠临场推理。

---

## 一、三个东西各是什么(先把参照物摆对)

```
   ┌─────────────────┐  ← canvas 顶 (y=0)
   │                 │
   │   canvas        │   canvas = 整张长内容板。装得下整个 background。
   │  (内容板)        │           它的 width/height 由【内容】声明,不是屏幕。
   │                 │           长条滚动页 = 一条长 strip(如 720×2242)。
   │  ┌───────────┐  │
   │  │  frame    │  │   frame  = 手机屏那"一屏"窗口,是 canvas 里的一个矩形。
   │  │ (取景窗口) │  │           canvas ≥ frame。frame 在 canvas 里自己排位置。
   │  └───────────┘  │
   └─────────────────┘  ← canvas 底 (y=canvas.height)
```

- **canvas**:整张板。所有元素、background 的绝对 px 都是相对**这块板的左上角**。
- **background**:一张图,像普通元素一样用 `x/y/w/h` **纯坐标**铺在板上。
- **frame**:手机可视的那一屏。它是**板里的一个窗口**,`env.frame.align` 决定它咬板的哪条边。

**铁律:三者各按自己的坐标铺,谁都不派生谁。**
背景怎么铺 = 背景自己的 `x/y/w/h`;frame 在哪 = frame 自己的 `x/y/w/h`。
**不存在"从 align 反推背景怎么画"** —— 那是把简单的事搞复杂(见 §五 教训)。

---

## 二、坐标系约定(符号别搞反)

- **左上角原点,y 向下为正。** `x/y` = 矩形**左上角**在 canvas 上的绝对 px。
- 一个矩形:`x`=左边距板左,`y`=顶边距板顶,`w/h`=宽高。
- **图顶边在板上方(溢出板外)→ y 是负数。** 图顶边在板内往下 → y 是正数。

---

## 三、背景宽锁高多少(唯一要算的一步)

背景默认**宽度锁定**填满板宽(720),高度按原图比例定:

```
displayH = nativeH × (canvas.width / nativeW)
```

例:endless-bg 原生 1120×3488 → `displayH = 3488 × 720/1120 = 2242`。

这一步**只依赖原图尺寸 + 板宽**,是纯算,不需要人给。人只给一件事:见 §四。

---

## 四、两类页面,坐标怎么落(照抄)

**唯一需要人判断的:这页背景比板"正好一样高"还是"更高(要滚)"。**

### A. 背景 = 板一样高(短页,一屏铺满)

home / shop / henhouse:背景宽锁后高 ≈ canvas 高。

```
canvas     = { width:720, height:H }        // H = displayH
background = { tex, x:0, y:0, w:720, h:H }   // 顶铺满,y=0
frame      = { x:0, y:0, w:720, h:H, align:'top' }  // frame=canvas,顶底重合
```

challenge 稍特殊:canvas(1989)比 frame 高,但背景 **top 类型**——顶着顶铺,
下面长出来的是滚动区。所以 **background.y 仍是 0**,frame 咬顶(align top)。
> 记牢:**top 类型 → 背景从顶铺 → y=0。** 长出来的部分在下方,不影响背景 y。

### B. 背景比板高,底对齐(长滚动页,bottom 类型)

endless:背景宽锁后 2242 高。**正确形态 = canvas 撑到内容总高,frame 排到底部那一屏。**

```
canvas     = { width:720, height:2242 }              // ← 板 = 内容总高(不是屏高!)
background = { tex, x:0, y:0, w:720, h:2242 }         // 顶铺满整张板,y=0
frame      = { x:0, y:682, w:720, h:1560, align:'bottom' }  // 手机屏那一屏排到板底部
                   └── frame.y = canvas.height − frame.height = 2242 − 1560 = 682(正数!)
```

**这就是 endless 那次搞反的点,记死:**
- 板(canvas)装下**整张**背景 → **background.y = 0**,不是负数。
- 非 0 的、需要 `+682` 的是 **frame.y**(手机框在长板里往下排到底部那一屏)。
- `frame.y = canvas.height − frame.height`,**正数**。
- ❌ 错法(助手犯过):以为 canvas=屏高(1560),把背景当成"顶部溢出"→ 给
  `background.y = −682`。这是**没把 canvas 声明成内容总高**导致的连环反号。
  只要记住"**canvas = 内容总高、背景永远 y=0 顶铺、frame 往下排**",符号不会反。

> 旧的 `fit:"width-bottom"` 字符串 = 上面这一坨的隐式版。已废弃(见 §五)。

---

## 五、为什么删了 `fit` / 不从 align 反推背景(教训)

历史上 background 带一个 `fit` 模式串(`width-top`/`width-bottom`/`contain`),
让渲染去**猜**背景怎么填。它编码了两件事:①宽锁 ②咬哪条边。owner 2026-07-13 定案干掉它:

- **①宽锁** = 纯算(§三),不用存。
- **②咬哪条边** = 本就是 **canvas/background/frame 的坐标关系**,坐标铺完自然可见,
  不需要一个模式串再说一遍。

删 fit 后一度想加个 `bgAlign()` 函数"从 `frame.align` 反推背景 top/bottom"——**这也是错的**。
背景铺哪 = 背景自己的 `x/y/w/h`,跟 frame.align 无关。frame.align 只管**取景窗口**
自己往哪延伸,不参与背景绘制。两者正交,不互相派生。

**结论:background 只认纯坐标 `{tex,x,y,w,h}`,走一条分支;没坐标就显示 checker。
所有 `fit`/`bgAlign`/contain 分支已删除。**

---

## 六、frame.align 三方向(center 已砍)

`env.frame.align` 只描述 **canvas 比 frame 高时,frame 咬板的哪条边 / 屏幕往哪延伸**:

| align | frame 咬板的 | 屏幕大小变化时往哪延伸 | 用例 |
|---|---|---|---|
| `top` | 顶边 (`frame.y=0`) | 只往**下**(内容向下滚) | home / challenge |
| `bottom` | 底边 (`frame.y=H−frameH`) | 只往**上** | endless |
| `baseline` | 顶边贴一条 `anchor-line` 元素 | **两边**,补多少由线定 | 母鸡坐窝那类 |

- **center 已删**:它 = `baseline` 且线在 50%,是冗余的第二种说法。
- **canvas = frame 时(短页),align 选啥都一样**(顶底重合),差别只在长页显现。
- align 是**页级**属性(整页取景方向)。**元素各自贴哪条边是另一层**——见 §七。

---

## 七、别和 element.anchor 混:两层对齐

- **`env.frame.align`(页级)**:整个取景窗口往哪延伸。一页一个。
- **`element.anchor`(元素级)**:每个元素屏幕适配时贴哪条边——`top`(贴屏幕顶/HUD)、
  `bottom`(贴屏幕底)、`baseline`(锚基准线)、**缺省 `none`**(固定随板走,不贴边)。

旧架构的"center/top/bottom 三锚点组"= 现在**逐元素 `anchor` 标注**,不再是分组结构。
母鸡/背景这类固定内容 **不标 anchor(none)**:有了绝对 px 板,它们拖对即焊死,
不需要锚任何线。frame 的 baseline 那条 `anchor-line` 是**给 frame 定位用的**,不是给母鸡用的。

---

## 关联

- [`absolute-contract.md`](./absolute-contract.md) — 字段级契约(canvas/background/elements/env/runtime)。
- [`three-layer-boundary.md`](./three-layer-boundary.md) — 三层边界(① Config 只存视觉事实,关系/行为归 ②/③)。
- 落地实现:`web/app.js` `applyCanvasBackground`(纯坐标分支)、`frameYForAlign`(三方向)。

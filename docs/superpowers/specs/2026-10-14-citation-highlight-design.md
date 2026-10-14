# 引用原文定位与高亮（点击引用 → 看到依据在原文档哪一行）

- 日期：2026-10-14
- 状态：已批准（设计已口头确认，spec 为落地依据）
- 范围：backend（rag 解析层 + 新定位模块 + 2 个只读端点）+ frontend（Sources 卡片按钮 + 右侧抽屉 + 4 个渲染器）
- 分支：`feat/citation-highlight`

## 1. 背景与问题

### 1.1 现状

引用今天只到"文档级 + 页级"，**到不了"行级"**。点引用走的是：

```
Citation（document_id / chunk_id / page_number / snippet）
  → Sources 面板里的 SourceRow（sources-tab.tsx:42）
  → 卡片上写「第 34 页 · 章节：8.5.1 · 片段 #12」
```

用户拿到"第 34 页"之后仍然要自己打开原件、翻到 34 页、再逐行找。**证据在哪一行，系统没有回答。**

### 1.2 已有的家底（决定了本设计的形态）

| 已有 | 位置 | 对本设计的意义 |
|---|---|---|
| 每个 chunk 的 `char_start` / `char_end` / `page` | `document_chunks.metadata_`，自 2026-09-19 起（迁移 0015，`chunk_meta.py`） | **定位所需的锚点库里早就有**，不需要回填 |
| 原件留在磁盘、可读 | `documents.file_path`，`_load_owned_doc` | 渲染时有原件可开 |
| "请求时重解析原件"已是既有模式 | `/documents/{id}/preview`（`documents.py:242`） | 现算坐标不引入新的架构风险 |
| `Citation.metadata` 是自由 dict | `schemas/chat.py:91` | 需要时可加字段而不破坏契约 |
| PyMuPDF 1.28 | `requirements.txt:46` | `get_text("dict")` / `draw_rect` / `get_pixmap` 全可用，**零新依赖** |

### 1.3 缺的是什么

**缺的不是坐标存储，是「chunk → 文档位置」这个映射能力。** 解析器今天把 `page.get_text("text")` 取走就扔掉了布局信息（`parsers.py:129`），字典模式下的 `lines[].bbox` 从未被读取。于是 char 区间在库里躺着，却没有任何东西能把它翻译成"页面上的哪几行"。

## 2. 目标与非目标

### 目标

- **G1** 点击引用的文档来源 → 在右侧抽屉里看到**原件中该 chunk 的确切位置**，四级精度分级降级：原页高亮框 / 表格行高亮 / 幻灯片形状高亮 / 原文文本片段高亮。
- **G2** 覆盖面 = 所有能产出文本的格式。PDF 到行级；表格到行级；幻灯片到形状级；其余到文本片段级。
- **G3** **零迁移、零重 embedding、零契约破坏**。老文档（有 char 偏移的）自动可用。
- **G4** 不支持的格式/文档**诚实降级**，不假装、不报错、不出错图。

### 非目标（明确划出）

- **不装 LibreOffice**。生产镜像今天没有它（`Dockerfile:13`），`.doc/.odt/.ods/.odp/.ppt` 连解析都失败，本设计不为它们铺路。若将来要覆盖，路径是给镜像加 LO 并复用同一条渲染链，届时应重新评估（见 §7.2）。
- **不做前端 PDF 渲染器（pdf.js）**。本轮走服务端出图，前端不引入 PDF 依赖。
- **不做附件（对话上传的大文件）高亮**。附件走临时 Qdrant 集合、没有 `document_chunks` 行、且其引用只出现在知识库设置页的「测试检索」里，**聊天中没有可点的引用入口**。本轮不做。
- **不做 DOCX 的"原页"高亮**。Word 无分页概念、python-docx 无布局坐标，真原页高亮只能靠 LO（见非目标第一条）。DOCX 落在 L3。
- **不做 OCR 页的坐标**。扫描页文本来自 Tesseract，没有可靠的版面坐标。
- **不改检索链路**。定位不参与召回、排序、rerank、门禁的任何一环。

## 3. 决策汇总

| # | 决策 | 选择 | 理由 / 被否的备选 |
|---|---|---|---|
| D1 | 坐标存哪 | **不存**；只存锚点 `char_start/char_end`（已有），请求时重解析现算 | 存下来要跟解析器版本走（`parsers.py` 高频变动），存量框会静默过期；char 锚点稳定。被否：写进 `metadata_` / 写进 Qdrant payload |
| D2 | 渲染在哪 | **服务端**，PyMuPDF 出整页 PNG | 前端零新依赖；坐标难题留在已有 PDF 处理能力的后端。被否：pdf.js 客户端渲染 |
| D3 | 框怎么给前端 | **归一化比例** `[x0/W, y0/H, x1/W, y1/H]` | 图可与 chunk 解耦、可长期 HTTP 缓存、缩放自动跟随、**坐标系翻转问题彻底消失**（比值与原点无关，也顺带解决 `/Rotate` 旋转页） |
| D4 | 页面图端点 | 返回**干净整页图、不带框**；框由 locator 单独给 | 同页多 chunk 复用一张图；图不依赖 chunk 才能用 `ETag` 长缓存 |
| D5 | 缓存 | HTTP `ETag` + `Cache-Control: private, max-age`，**不做服务端缓存** | 图对给定 (文档, 页, dpi) 永不变化，浏览器缓存足够；省掉缓存失效管理 |
| D6 | 协议形态 | 一个 `Locator`，`kind` 分派四种渲染器 | 加格式不动协议。被否：每格式一个端点 |
| D7 | 文本级怎么给 | 后端返回**带上下文的窗口** + 窗口内偏移 | 偏移相对 `parsed.text`，而预览接口返回 Markdown，两者对不上；让前端对齐必然错 |
| D8 | 入口在哪 | Sources 卡片上的「查看原文位置」按钮，条件：`chunk_id != null` 且来源是文档 | 不需要知道能力矩阵；不可用时按 D9 降级 |
| D9 | 不支持时的行为 | 按钮**照常显示**，点了给明确文案「该文档无法定位到原文」 | 悲观隐藏按钮会让用户以为功能没做；乐观显示+诚实文案更符合本项目"不把没查伪装成查过"的一贯取向 |
| D10 | 展示容器 | 右侧抽屉（侧边栏），覆盖在 Context Panel 之上 | 居中遮罩层宽度不受侧栏限制但打断上下文；侧栏与既有面板体系一致，关闭即回原处 |
| D11 | 老文档 | **自动可用**（不需要回填） | `char_start` 自 2026-09-19 起就在库里；缺的自动降级 |

## 4. 架构

### 4.1 数据流

```
① 入库（一次性，新增产出一项）
   解析器 → ParsedDocument.text（不变）
          → ParsedDocument.layout  ← 新增：布局索引（页/行/框 或 行/单元格 或 幻灯片形状）
   chunk_meta.annotate → char_start/char_end/page（不变）

② 点击（请求时）
   Citation(document_id, chunk_id) → Sources 卡片按钮
     → GET /documents/{id}/chunks/{chunk_id}/locator
         ‣ 鉴权（_load_owned_doc）
         ‣ 读 document_chunks.metadata_ 拿 char_start/char_end/page
         ‣ 重解析原件（asyncio.to_thread）→ ParsedDocument(+layout)
         ‣ locator.locate(parsed, page=, char_start=, char_end=) → Locator
         ‣ 按 kind 组织响应（page_image 只给页码+归一化框；其余自带内容）
     → 抽屉按 kind 分派渲染器
     → kind=page_image 时再 GET /documents/{id}/pages/{n}/image?dpi=150（可长缓存）
```

### 4.2 为什么请求时重解析是可接受的

- 仓库已有先例（`/documents/{id}/preview` 每次请求都 `default_parser.parse`）。
- 只读、无副作用、无 LLM 调用、无 embedding 调用。
- 成本 = 一次 PyMuPDF 文本+布局提取（A4 百页量级 ~0.3~1.5s），由 `asyncio.to_thread` 隔离，不阻塞事件循环。
- **收益是决定性的**：永远与当前解析器自洽，且老文档零成本可用。

### 4.3 模块边界

| 模块 | 职责 | 依赖 |
|---|---|---|
| `app/rag/layout.py` | 布局索引的数据结构与角色：`LineBox` / `PageLines` / `SheetLayout` / `SlideLayout` / `DocumentLayout`。**只描述数据，不做定位** | 无（纯 dataclass） |
| `app/rag/locator.py` | `locate(parsed, page, char_start, char_end) -> Locator \| None`；四种 Locator 变体与挑选逻辑。**只做定位，不碰 HTTP、不碰 DB** | `layout`、`base.ParsedDocument` |
| `app/api/documents.py`（扩） | 两个端点 + 鉴权 + 参数校验 + ETag | `locator`、`parsers`、`documents` 模型 |
| `frontend/src/lib/locator.ts` | Locator 类型 + 归一化框 → CSS 百分比换算（纯函数） | 无 |
| `frontend/src/components/context/highlight-drawer.tsx` | 抽屉壳 + kind 分派 | `locator.ts`、`api.ts` |
| `frontend/src/components/context/locators/*.tsx` | 四个渲染器，各管一种 kind | — |

## 5. 各层落地

### 5.1 解析层：`ParsedDocument.layout`

`base.ParsedDocument` 新增一个字段，**默认 `None`**，只有能产出布局的解析器才填：

```python
layout: DocumentLayout | None = None
```

布局索引的数据形态按格式分化，但共同点是**每一条布局单元都带它在 `parsed.text` 里的字符区间** —— 这是与既有 `char_start/char_end` 对齐的唯一接口。

**PDF（L1）**：`DocumentLayout.pages: dict[int, PageLines]`，`PageLines` 含页宽高 + `list[LineBox]`，`LineBox` 含 `text` 与 `x0/y0/x1/y1`（PyMuPDF 坐标：左上原点，单位 pt）。

行文本来自 `page.get_text("dict")` 的 blocks→lines→spans 拼接。**行在 `parsed.text` 里的偏移不靠重建等式，靠前向 search 恢复**（`chunk_meta._offsets` 同款技术）：对每一行，在真实页文本里从游标处 `find`；找不到的行**丢弃**（该行不可高亮），不猜测、不错位。已实测：合成样本上 dict 重建与 `text` 模式逐页一致，但真实 PDF 必然更脏，所以不依赖这个等式。

**XLSX/XLS/CSV（L2）**：`DocumentLayout.sheets: list[SheetLayout]`，每行记 `(start, end, row_index)`。`_df_to_text` 渲染行时顺带记偏移。

**PPTX（L2）**：`DocumentLayout.slides: list[SlideLayout]`，每形状记 `(rect, text_char_start, text_char_end)`，rect 由 EMU 换算为 pt；另存幻灯片宽高。

**其余格式**：`layout = None`，走 L3（L3 不需要布局，只需要 `parsed.text` 与 char 区间）。

### 5.2 定位层：四种 Locator

```python
PageImageLocator(kind="page_image", page, total_pages, boxes: list[tuple[float,float,float,float]])
SheetRangeLocator(kind="sheet_range", sheet, header, rows, highlight: tuple[int,int])
SlideLocator(kind="slide", slide_no, width, height, shapes: list[ShapeRect])
TextRangeLocator(kind="text_range", text, start, end)
```

挑选顺序（**最精确的优先**）：

1. 有 `layout.pages` 且 chunk 的 page 有布局 → `page_image`：取该页中与 `[char_start, char_end)` 相交的所有行 → **每行一个框，返回框的列表；绝不把多行并成一个矩形**（并成一个大矩形就等于把相邻 chunk 的内容也框进去，正是本功能要避免的谎言）→ 逐个归一化
2. 有 `layout.sheets` 且 char 区间落在某 sheet 内 → `sheet_range`：相交的行区间
3. 有 `layout.slides` 且 char 区间落在某 slide 内 → `slide`：相交的形状
4. 否则 → `text_range`：取 `[start-CONTEXT, end+CONTEXT)` 的窗口（`CONTEXT=800` 字）

**任何一步定位失败（无 char 偏移、无页号、找不到相交单元）都返回 `None`**，端点转成机器可读的 `code=no_locator`，前端按 D9 出文案。

### 5.3 端点层

```
GET /api/documents/{document_id}/chunks/{chunk_id}/locator
    200 → Locator JSON
    404 code=no_locator     该 chunk 无法定位（老数据无 char 偏移 / 扫描页 / 格式无布局且无可切窗口）
    404 code=storage_gone   原件已清理
    409                     文档解析中
    500                     解析失败

GET /api/documents/{document_id}/pages/{page}/image?dpi=150
    200 → image/png，带 ETag（doc.content_sha256 + page + dpi）与 Cache-Control: private, max-age=86400
    404 code=storage_gone / 页码越界
```

两个端点**都是只读、都要过 `_load_owned_doc`**（不区分"不存在"与"不是你的"，避免变成文档探测器——沿用 `retrieval.py:86` 的同款理由）。

`dpi` 限幅（72~300），避免构造超大图打爆内存。单页渲染前检查页宽高上限。

### 5.4 前端

**入口**：`sources-tab.tsx` 的 `SourceRow`，当 `c.chunk_id` 存在且来源是文档时渲染「查看原文位置」按钮。

**抽屉**：右侧滑入，宽 `min(720px, 60vw)`，覆盖在 Context Panel 之上，关闭回原处。打开时拉 locator，展示：

- `page_image`：`<img>`（整页图）+ 绝对定位的高亮 `<div>`（`left/top/width/height` 用百分比，直接来自归一化框）+ 页码/总页数 + 缩放控件（CSS transform，框自动跟随）
- `sheet_range`：表格 + 高亮行（背景色）
- `slide`：SVG，灰底画全部形状框、命中形状填黄；文本用 `<foreignObject>` 或 SVG `<text>` 分行
- `text_range`：`<pre>` 风格原文窗口，命中区间包 `<mark>`

**归一化换算是纯函数**，放 `lib/locator.ts`，单独测。

## 6. 测试策略

**后端**

1. `test_layout.py`：用 PyMuPDF 现场生成 fixture PDF，断言 (a) 行偏移单调递增且能在页文本里 `find` 到，(b) 每行 bbox 落在页矩形内，(c) 找不到的行被丢弃而不是错位。
2. `test_locator.py`：四种 kind 各自的挑选与降级——无 char 偏移 → `None`；page 无布局 → 退回 `text_range`；表格行区间边界（首行/末行/跨行）；归一化框值域在 `[0,1]` 且 `x0<x1, y0<y1`。
3. `test_documents_locator_api.py`：鉴权（他人文档 404）、`no_locator` 码、`storage_gone` 码、解析中 409、ETag 命中（304）、dpi 限幅。
4. fixture 用 `pymupdf` 生成 PDF、`openpyxl` 生成 xlsx、`python-pptx` 生成 pptx，**不向仓库提交二进制**。

**前端**

5. `lib/__tests__/locator.test.ts`：归一化换算、框排序、窗口高亮切分。
6. `highlight-drawer` 的 kind 分派与降级文案（vitest + testing-library，随既有 `src/lib/__tests__` 风格）。

**回归**

7. 跑仓库既有 RAG 测试，确认 `ParsedDocument` 加字段与解析器改动**没有改变 `text`**（chunking/embedding 是内容敏感的，text 变了就是隐性重索引）。这一条是本改动最大的回归风险，必须有断言。

## 7. 风险与降级

### 7.1 已知会降级的情形（都要在文案里说清，不能静默）

| 情形 | 表现 |
|---|---|
| 扫描件 OCR 页 | 文本来自 Tesseract，无版面坐标 → `text_range` |
| 表格被折进正文尾部（`_parse_pdf` 把 tables 追加在 pages 之后） | 该区域无页号 → `text_range` |
| 2026-09-19 之前入库的 chunk | `metadata_` 无 `char_start` → `no_locator` |
| `.doc/.odt/.ods/.odp/.ppt` | 生产解析即失败，本就不产生引用 |
| DOCX/RTF/HTML/EPUB | 无布局 → `text_range`（设计如此，不是缺陷） |

### 7.2 LibreOffice 那条路（将来若要覆盖 Office 原页高亮）

代价必须摊开：镜像 +300~600MB；转换冷启 5~10s 故必须在**入库时**转换并**存下转出的 PDF**（原件 2~3 倍大）；且 **LO 的分页 ≠ Word 的分页**（字体缺失会改变行宽与分页），届时给用户的高亮将是"LO 那一版的第 N 页"。**这是一个会误导用户的谎言，不应悄悄上线**——要做必须先决定如何向用户表述。

### 7.3 PyMuPDF 处理不可信 PDF

PDF 是用户上传的不可信输入，PyMuPDF 是 C 扩展。单页渲染加尺寸上限与页数校验（已列 §5.3），异常一律转 500 而不是崩溃进程。

## 8. 验收

- 点击 PDF 引用的文档来源 → 抽屉里出现该页原图，**高亮框精确覆盖该 chunk 的内容行**，且不覆盖属于相邻 chunk 的行。
- 点击 XLSX 引用 → 抽屉里出现该工作表的表格，**命中行被高亮**。
- 点击 PPTX 引用 → 抽屉里出现该幻灯片 SVG，**命中形状被高亮**。
- 点击 DOCX/TXT/MD 引用 → 抽屉里出现原文窗口，**命中片段被 `<mark>`**。
- 老文档（2026-09-19 之后入库）点击同样可用，**无需任何回填或重新索引**。
- 不支持的文档点击 → 明确文案，无报错、无空白。
- 仓库既有测试全绿；`ruff check app tests` 全绿；前端 `typecheck / lint / test` 全绿。
- **`ParsedDocument.text` 对既有格式逐字节不变**（回归断言）。

/**
 * 引用原文定位：后端给的 Locator 形状，以及把它渲染出来所需的纯换算。
 *
 * 高亮框是**归一化比例**（相对页面宽高的 0~1 值），不是像素。这样图与框解耦：
 * 同一页被多个引用命中时图只渲染一次，抽屉缩放时框自动跟随，也不需要任何
 * "坐标系翻转" —— 比例与原点在哪没关系。
 */

export interface PageImageLocator {
  kind: "page_image";
  page: number;
  total_pages: number;
  image_url: string;
  /** 归一化 [x0, y0, x1, y1]，一行一个，绝不合并。 */
  boxes: number[][];
}

export interface SheetRangeLocator {
  kind: "sheet_range";
  sheet: string;
  header: string[];
  rows: string[][];
  /** [起, 止)，相对 rows（表头不算行）。 */
  highlight: [number, number] | null;
}

export interface SlideShape {
  rect: [number, number, number, number];
  text: string;
  hit: boolean;
}

export interface SlideLocator {
  kind: "slide";
  slide_no: number;
  width: number;
  height: number;
  shapes: SlideShape[];
}

export interface TextRangeLocator {
  kind: "text_range";
  text: string;
  start: number;
  end: number;
}

export type Locator =
  | PageImageLocator
  | SheetRangeLocator
  | SlideLocator
  | TextRangeLocator;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

/**
 * 比例 → CSS 百分比字符串。
 *
 * 必须取整：`0.6 - 0.5` 是 `0.09999999999999998`，直接乘 100 会写出
 * `9.999999999999998%`。视觉上没差别，但会让每个断言和快照都被浮点噪声弄脏。
 * 取到小数后 4 位，精度远超任何显示器能体现的差别。
 */
function percent(value: number): string {
  return `${Math.round(value * 100 * 1e4) / 1e4}%`;
}

/** 归一化框 → CSS 百分比。越界值必须夹住，否则框会画到页面外。 */
export function toPercentBox(box: number[]): {
  left: string;
  top: string;
  width: string;
  height: string;
} {
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = (box || []).map(clamp01);
  return {
    left: percent(x0),
    top: percent(y0),
    width: percent(Math.max(0, x1 - x0)),
    height: percent(Math.max(0, y1 - y0)),
  };
}

/** 高亮行号列表。`highlight` 缺失或非法时给空数组 —— 渲染层不该为此崩。 */
export function highlightedRows(loc: SheetRangeLocator): number[] {
  const range = loc.highlight;
  const rowCount = Array.isArray(loc.rows) ? loc.rows.length : 0;
  if (!Array.isArray(range) || range.length !== 2) return [];
  const [start, end] = range;
  const out: number[] = [];
  for (let i = Math.max(0, start); i < end && i < rowCount; i += 1) {
    out.push(i);
  }
  return out;
}

/**
 * 文本窗口切成 前 / 命中 / 后 三段。
 * 后端给的 start/end 是相对窗口的偏移；越界时按窗口长度夹住，保证三段拼回去
 * 仍等于原文（拼接丢失就等于悄悄改了用户的原文）。
 */
export function splitTextRange(loc: TextRangeLocator): [string, string, string] {
  const length = loc.text.length;
  const start = Math.max(0, Math.min(loc.start, length));
  const end = Math.max(start, Math.min(loc.end, length));
  return [loc.text.slice(0, start), loc.text.slice(start, end), loc.text.slice(end)];
}

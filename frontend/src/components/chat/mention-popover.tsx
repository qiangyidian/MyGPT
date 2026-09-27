"use client";

/** composer 的 ``@`` 引用候选弹层（条目 26 的 UI 半边）。
 *
 * 规则本身不在这里：判定「正在输入一条引用」、插入 token、上限裁决全是
 * `@/lib/inline-refs` + `@/lib/mention-insert` 的纯函数（没有 DOM，所以能在 node
 * 环境单测）。本文件只做三件必须在浏览器里做的事：拉候选、走高亮、把选中目标交回
 * composer。
 *
 * 两个容易做错、所以写明白的点：
 * - 防抖 ≥250ms + 序号丢弃：中文查询每个音节都会变，没有防抖就是在刷
 *   ``rate_limit_user(120, 60)``；没有序号丢弃，慢的那次响应会盖掉新查询的结果。
 * - 输入法：合成期间（compositionstart…compositionend）composer 不会喂文本进来，
 *   拼音候选串也就不该被当成引用查询。
 */
import {
  type KeyboardEvent,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Check, Database, FileText, Loader2, Paperclip } from "lucide-react";

import { api } from "@/lib/api";
import {
  REF_KIND_LABEL,
  findMentionAt,
  type MentionQuery,
} from "@/lib/inline-refs";
import {
  FALLBACK_LIMITS,
  limitsFromMentionList,
  type MentionLimits,
} from "@/lib/mention-insert";
import type { MentionTarget } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 候选请求防抖：中文每个音节都会改查询串，别每次都打服务端。 */
const DEBOUNCE_MS = 260;
/** 弹层里最多画几条（服务端另有 limit，这里只管高度）。 */
const MAX_VISIBLE = 8;

function KindIcon({ kind }: { kind: MentionTarget["kind"] }) {
  const cls = "h-3.5 w-3.5 shrink-0 text-muted-foreground";
  if (kind === "kb") return <Database className={cls} />;
  if (kind === "doc") return <FileText className={cls} />;
  return <Paperclip className={cls} />;
}

/** ``selectable:false`` 的那几条要能自报家门：只灰不给理由，用户只会反复点。 */
function unselectableReason(target: MentionTarget): string {
  if (target.kind === "file") return "附件已删除，无法引用";
  const state = (target.sublabel ?? "").split("·").pop()?.trim() ?? "";
  if (!state) return "暂不可检索";
  return `还在${state}，暂不可引用`;
}

export interface MentionPopoverOptions {
  /** 传进端点才有「本对话附件」那一路候选。 */
  conversationId: string | null;
  /** 光标由宿主持有：读实时 selectionStart，别信闭包里的旧值。 */
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** 落子交回 composer：由纯函数 insertMention 决定正文、光标与拒绝原因。 */
  onPick: (target: MentionTarget, anchor: MentionQuery, caret: number) => void;
  /** 服务端随候选下发的上限，composer 插入时要用同一份。 */
  onLimitsChange?: (limits: MentionLimits) => void;
}

export interface MentionPopoverState {
  open: boolean;
  anchor: MentionQuery | null;
  items: MentionTarget[];
  active: number;
  loading: boolean;
  failed: boolean;
  truncated: boolean;
  limits: MentionLimits;
  /** composer 每次文本/光标变化后调用（合成期间不要调）。 */
  sync: (text: string, caret: number) => void;
  /** 回 true 表示按键已被弹层吃掉（回车是落子，不是发送）。 */
  handleKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  close: () => void;
  pick: (target: MentionTarget) => void;
  setActive: (index: number) => void;
}

/** ``@`` 弹层的状态机；渲染交给 <MentionPopover/>。 */
export function useMentionPopover({
  conversationId,
  textareaRef,
  onPick,
  onLimitsChange,
}: MentionPopoverOptions): MentionPopoverState {
  const [anchor, setAnchor] = useState<MentionQuery | null>(null);
  /** 已防抖落定的查询串（null = 弹层刚开、这一轮还没防抖完）。 */
  const [term, setTerm] = useState<string | null>(null);
  const [items, setItems] = useState<MentionTarget[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [limits, setLimits] = useState<MentionLimits>(FALLBACK_LIMITS);
  const [active, setActive] = useState(0);
  /** 只接受最后一次发出的响应：慢一步的旧查询不许把结果盖回来。 */
  const seq = useRef(0);

  useEffect(() => {
    if (!anchor) {
      setTerm(null);
      return;
    }
    const timer = setTimeout(() => setTerm(anchor.query), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [anchor]);

  const open = !!anchor;

  // 弹层关着就不请求（用户按 Esc 之后不该再为此付一次配额）。
  useEffect(() => {
    if (!open || term === null) return;
    const my = (seq.current += 1);
    const query = term;
    setLoading(true);
    setFailed(false);
    api
      .searchMentions(query, conversationId)
      .then((res) => {
        if (my !== seq.current) return;
        setItems(res.items ?? []);
        setTruncated(!!res.truncated);
        const next = limitsFromMentionList(res);
        setLimits(next);
        onLimitsChange?.(next);
      })
      .catch(() => {
        if (my !== seq.current) return;
        setItems([]);
        setTruncated(false);
        setFailed(true);
      })
      .finally(() => {
        if (my === seq.current) setLoading(false);
      });
    // onLimitsChange 是宿主每次渲染新建的闭包，放进依赖会反复重发请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, term, conversationId]);

  const sync = useCallback((text: string, caret: number) => {
    const next = findMentionAt(text, caret);
    // 同一条引用不必换对象：anchor 一变防抖就重来一拍，光标原地抖动会饿死请求。
    setAnchor((prev) =>
      prev?.start === next?.start &&
      prev?.end === next?.end &&
      prev?.query === next?.query
        ? prev
        : next,
    );
  }, []);

  const close = useCallback(() => {
    setAnchor(null);
    textareaRef.current?.focus();
  }, [textareaRef]);

  // 请求在路上时先拿上一轮结果按当前串本地过一遍：列表不闪空，也不会把
  // 「@产品手册」已经敲出来、却和它不搭的候选留在窗里。
  const visible = useMemo(() => {
    const query = (anchor?.query ?? "").toLowerCase();
    const scoped = query
      ? items.filter((t) => {
          const hay = `${t.label} ${t.sublabel ?? ""}`.toLowerCase();
          return hay.includes(query);
        })
      : items;
    return scoped.slice(0, MAX_VISIBLE);
  }, [items, anchor]);

  // 候选集变了就把高亮夹回范围，并跳到第一条可选的（禁选项不参与高亮）。
  useEffect(() => {
    setActive((i) => {
      if (visible.length === 0) return 0;
      const clamped = Math.min(i, visible.length - 1);
      if (visible[clamped]?.selectable === false) {
        return nextSelectable(visible, clamped, 1) ?? clamped;
      }
      return clamped;
    });
  }, [visible]);

  const pick = useCallback(
    (target: MentionTarget) => {
      if (target.selectable === false) return;
      const el = textareaRef.current;
      const caret = el?.selectionStart ?? anchor?.end ?? 0;
      const at = anchor;
      setAnchor(null);
      if (!at) return;
      onPick(target, at, caret);
    },
    [anchor, onPick, textareaRef],
  );

  const handleKeyDown = useCallback<MentionPopoverState["handleKeyDown"]>(
    (e) => {
      if (!anchor) return false;
      // 合成期间的按键归输入法的候选框，弹层不抢。
      if (e.nativeEvent?.isComposing) return false;
      const usable = visible
        .map((t, i) => ({ t, i }))
        .filter(({ t }) => t.selectable !== false);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!usable.length) return true;
        const step = e.key === "ArrowDown" ? 1 : -1;
        const next = nextSelectable(visible, active, step);
        setActive(next ?? active);
        return true;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return true;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        // 弹层开着时回车是「落子」，不是「发出去」。
        e.preventDefault();
        const target = visible[active];
        if (target && target.selectable !== false) pick(target);
        else close();
        return true;
      }
      return false;
    },
    [active, anchor, close, pick, visible],
  );

  return {
    open,
    anchor,
    items: visible,
    active,
    // 防抖那一拍还没轮到发请求时也算「在忙」：否则空窗会先闪一句「没有匹配」。
    loading: loading || (open && term === null),
    failed,
    truncated,
    limits,
    sync,
    handleKeyDown,
    close,
    pick,
    setActive,
  };
}

/** 从 ``from`` 起按 ``step`` 找下一条可选候选（环形）；一条都不可选时回 null。 */
function nextSelectable(
  items: readonly MentionTarget[],
  from: number,
  step: number,
): number | null {
  const usable = items
    .map((t, i) => (t.selectable === false ? -1 : i))
    .filter((i) => i >= 0);
  if (!usable.length) return null;
  const at = usable.indexOf(from);
  if (at === -1) {
    // 当前高亮落在禁选项（或已消失）上：从最靠近它的那条可选项接上。
    return usable.reduce(
      (best, i) =>
        Math.abs(i - from) < Math.abs(best - from) ? i : best,
      usable[0] ?? 0,
    );
  }
  const nextIndex =
    ((at + step) % usable.length + usable.length) % usable.length;
  return usable[nextIndex] ?? null;
}

interface MentionPopoverProps {
  state: MentionPopoverState;
  className?: string;
}

/** 受控弹层：↑/↓ 与回车已在 hook 里处理，这里只画出来 + 接鼠标点选。 */
export function MentionPopover({ state, className }: MentionPopoverProps) {
  if (!state.open) return null;
  const query = state.anchor?.query ?? "";
  return (
    <div
      role="listbox"
      aria-label="引用候选"
      className={cn(
        "absolute bottom-full left-0 z-20 mb-2 w-[min(380px,80vw)] overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md",
        className,
      )}
    >
      <div className="max-h-[248px] overflow-y-auto p-1">
        {state.loading && state.items.length === 0 && (
          <p className="px-2 py-3 text-xs text-muted-foreground">正在查找引用…</p>
        )}
        {!state.loading && state.failed && (
          <p className="px-2 py-3 text-xs text-destructive">
            引用候选加载失败，请稍后重试；也可以直接发送，服务端仍会按正文里的
            @ 引用检索。
          </p>
        )}
        {!state.loading && !state.failed && state.items.length === 0 && (
          <p className="px-2 py-3 text-xs text-muted-foreground">
            {query
              ? `没有匹配「${query}」的知识库、文档或本对话附件。`
              : "还没有可引用的知识库、文档或本对话附件。"}
          </p>
        )}
        {state.items.map((target, i) => {
          const disabled = target.selectable === false;
          const reason = disabled ? unselectableReason(target) : null;
          return (
            <button
              key={`${target.kind}:${target.id}`}
              type="button"
              role="option"
              aria-selected={i === state.active}
              aria-disabled={disabled || undefined}
              // 别把焦点从 textarea 抢走：光标一挪这条就被判为「不在引用中」。
              onMouseDown={(e) => e.preventDefault()}
              onMouseMove={() => {
                if (!disabled) state.setActive(i);
              }}
              onClick={() => state.pick(target)}
              className={cn(
                "flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm",
                disabled ? "cursor-not-allowed opacity-55" : "hover:bg-accent",
                i === state.active && !disabled && "bg-accent",
              )}
              title={reason ? `${target.label}（${reason}）` : target.label}
            >
              <KindIcon kind={target.kind} />
              <span className="min-w-0 flex-1">
                <span className="block truncate">{target.label}</span>
                <span
                  className={cn(
                    "block truncate text-[11px] text-muted-foreground",
                    reason && "text-destructive",
                  )}
                >
                  {reason ?? `${REF_KIND_LABEL[target.kind]} · ${target.sublabel ?? ""}`}
                </span>
              </span>
              {i === state.active && !disabled ? (
                <Check className="mt-1 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              ) : null}
            </button>
          );
        })}
      </div>
      <p className="flex items-center gap-1 border-t px-2 py-1 text-[11px] text-muted-foreground">
        {state.loading ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        <span>
          {state.truncated
            ? "候选较多，仅显示一部分：继续输入可缩小范围。↑/↓ 选择，Enter 插入，Esc 关闭。"
            : "↑/↓ 选择，Enter 插入，Esc 关闭。"}
        </span>
      </p>
    </div>
  );
}

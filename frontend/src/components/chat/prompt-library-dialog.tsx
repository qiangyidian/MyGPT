"use client";

/**
 * 提示词库选择器（条目 34）。
 *
 * 三件事：浏览/搜索（预置 + 我的）→ 预览正文 → 把渲染后的文本交给调用方。
 *
 * 占位符的识别与替换全部走 `@/lib/prompt-apply` 的纯函数，所以「将要发出去的确切
 * 文本」在插入前就摊在用户眼前；没填的占位符**保留原样**并禁用插入按钮，而不是静默
 * 留个 `{{代码}}` 给模型去猜（服务端刻意不做替换，见 `app/models/prompt_template.py`）。
 *
 * 筛选与排序在客户端做：一次拉一页（后端 `limit` 上限 200）后用 `searchFilter`
 * 即时筛，中文输入不必每敲一个字发一次请求；分类列表来自
 * `GET /api/prompts/categories`，用户自建分类能立刻出现。
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, Sparkles } from "lucide-react";

import { api } from "@/lib/api";
import {
  PROMPT_CATEGORIES_QUERY_KEY,
  PROMPTS_QUERY_KEY,
  extractPlaceholders,
  interpolateTemplate,
  isPresetPrompt,
  searchFilter,
  sortPromptGroups,
  type PromptScope,
} from "@/lib/prompt-apply";
import { userErrorMessage } from "@/lib/api-error";
import type { PromptTemplate } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/** 后端 `MAX_PAGE_SIZE` 就是 200，一次取满，页内再本地筛。 */
const PICKER_PAGE_SIZE = 200;

/** Radix 的 Select 不许 value 为空串，所以「全部分类」用哨兵值。 */
const ALL_CATEGORIES = "__all__";

const SCOPES: Array<{ value: PromptScope; label: string }> = [
  { value: "all", label: "全部" },
  { value: "preset", label: "平台预置" },
  { value: "mine", label: "我的模板" },
];

export interface PromptPickMeta {
  id: string;
  title: string;
}

export interface PromptLibraryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 交出插值后的文本；由调用方决定是填进输入框还是直接发送。 */
  onPick: (text: string, meta: PromptPickMeta) => void;
}

export function PromptLibraryDialog({
  open,
  onOpenChange,
  onPick,
}: PromptLibraryDialogProps) {
  const [scope, setScope] = useState<PromptScope>("all");
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});

  // 关掉就清空一次性状态：否则上次选中的模板/填过的变量会串进下一次打开。
  useEffect(() => {
    if (!open) {
      setSelectedId(null);
      setValues({});
    }
  }, [open]);

  const { data: rows, isLoading, error } = useQuery({
    queryKey: [...PROMPTS_QUERY_KEY, "library", scope, category],
    queryFn: () =>
      api.listPrompts({
        scope,
        category: category || undefined,
        limit: PICKER_PAGE_SIZE,
      }),
    enabled: open,
    staleTime: 30_000,
  });

  const { data: categories } = useQuery({
    queryKey: [...PROMPT_CATEGORIES_QUERY_KEY, scope],
    queryFn: () => api.listPromptCategories(scope),
    enabled: open,
    staleTime: 30_000,
  });

  const list = useMemo(
    () => sortPromptGroups(searchFilter(rows ?? [], { q, scope })),
    [rows, q, scope]
  );

  // 选中的模板从原始页里找（不从未命中的筛选结果里找）：填了一半占位符时改一下
  // 搜索词，右边的表单不该因此凭空消失。
  const selected = useMemo(
    () => (rows ?? []).find((p) => p.id === selectedId) ?? null,
    [rows, selectedId]
  );
  const placeholders = useMemo(
    () => (selected ? extractPlaceholders(selected.content) : []),
    [selected]
  );
  const rendered = useMemo(
    () => (selected ? interpolateTemplate(selected.content, values) : null),
    [selected, values]
  );
  const missing = rendered?.missing ?? [];

  function handlePick() {
    if (!selected || !rendered || missing.length > 0) return;
    onPick(rendered.text, { id: selected.id, title: selected.title });
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[85vh] w-full max-w-4xl flex-col gap-0 overflow-hidden p-0 sm:max-w-4xl">
        <DialogHeader className="flex-none space-y-1 border-b border-border px-6 py-4">
          <DialogTitle className="flex items-center gap-2 pr-8 text-base">
            <Sparkles className="h-4 w-4 shrink-0 text-muted-foreground" />
            提示词库
          </DialogTitle>
          <DialogDescription className="text-xs">
            挑一个模板，填好里面的占位符，确认预览无误后插入输入框。
          </DialogDescription>
        </DialogHeader>

        {/* 筛选条 */}
        <div className="flex-none space-y-2 border-b border-border px-6 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-0 flex-1">
              <div className="relative max-w-sm">
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="搜索标题、正文或标签…"
                  aria-label="搜索提示词"
                  className="h-9 pl-9"
                />
              </div>
            </div>
            <Select
              value={category || ALL_CATEGORIES}
              onValueChange={(v) => setCategory(v === ALL_CATEGORIES ? "" : v)}
            >
              <SelectTrigger className="h-9 w-[150px]" aria-label="按分类筛选">
                <SelectValue placeholder="全部分类" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_CATEGORIES}>全部分类</SelectItem>
                {(categories ?? []).map((c) => (
                  <SelectItem key={c} value={c}>
                    {c}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="flex items-center gap-1 rounded-md border border-input p-0.5">
              {SCOPES.map((s) => (
                <button
                  key={s.value}
                  type="button"
                  onClick={() => setScope(s.value)}
                  aria-pressed={scope === s.value}
                  className={cn(
                    "rounded px-2.5 py-1 text-xs transition-colors",
                    scope === s.value
                      ? "bg-secondary font-medium text-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[minmax(0,300px)_minmax(0,1fr)]">
          {/* 列表 */}
          <div className="min-h-0 overflow-y-auto border-b border-border md:border-b-0 md:border-r">
            {isLoading && (
              <div className="space-y-2 p-4">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full" />
                ))}
              </div>
            )}
            {!isLoading && error && (
              <p className="p-4 text-sm text-destructive">
                加载失败：{userErrorMessage(error)}
              </p>
            )}
            {!isLoading && !error && list.length === 0 && (
              <p className="p-4 text-sm text-muted-foreground">
                {rows && rows.length > 0
                  ? "没有匹配的提示词，试试换个关键词。"
                  : "这里还没有提示词。可以在「设置 → 提示词库」里新建。"}
              </p>
            )}
            {!isLoading &&
              list.map((prompt) => (
                <PromptRow
                  key={prompt.id}
                  prompt={prompt}
                  active={prompt.id === selectedId}
                  onSelect={() => {
                    setSelectedId(prompt.id);
                    setValues({});
                  }}
                />
              ))}
          </div>

          {/* 预览 + 占位符表单 */}
          <div className="min-h-0 overflow-y-auto p-6">
            {!selected ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
                <Sparkles className="h-8 w-8 text-muted-foreground/50" />
                <p className="text-sm text-muted-foreground">
                  从左边选一个提示词即可查看正文并填写占位符。
                </p>
              </div>
            ) : (
              <div className="space-y-5">
                <div>
                  <h3 className="text-sm font-semibold">{selected.title}</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {selected.category}
                    {selected.description ? ` · ${selected.description}` : ""}
                  </p>
                </div>

                {placeholders.length > 0 && (
                  <div className="space-y-3 rounded-lg border border-border bg-muted/30 p-4">
                    <p className="text-xs text-muted-foreground">
                      这个模板有 {placeholders.length} 个占位符，填了才会替换：
                    </p>
                    {placeholders.map((name) => (
                      <div key={name} className="grid gap-1.5">
                        <Label htmlFor={`prompt-ph-${name}`} className="text-xs">
                          {name}
                        </Label>
                        <Textarea
                          id={`prompt-ph-${name}`}
                          value={values[name] ?? ""}
                          rows={2}
                          className="min-h-0 text-sm"
                          placeholder={`请输入「${name}」的内容`}
                          onChange={(e) =>
                            setValues((prev) => ({ ...prev, [name]: e.target.value }))
                          }
                        />
                      </div>
                    ))}
                    {missing.length > 0 && (
                      <p className="text-xs text-destructive">
                        还需填写：{missing.join("、")}
                      </p>
                    )}
                  </div>
                )}

                <div className="space-y-1.5">
                  <p className="text-xs text-muted-foreground">插入后的内容预览</p>
                  <pre className="whitespace-pre-wrap break-words rounded-lg border border-border bg-card p-4 font-mono text-[13px] leading-relaxed">
                    {rendered?.text ?? ""}
                  </pre>
                </div>
              </div>
            )}
          </div>
        </div>

        <DialogFooter className="flex-none items-center gap-2 border-t border-border px-6 py-3 sm:justify-between">
          <span className="text-xs text-muted-foreground">
            {selected
              ? `已选「${selected.title}」${missing.length > 0 ? " · 占位符未填完" : ""}`
              : "未选择提示词"}
          </span>
          <span className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button size="sm" onClick={handlePick} disabled={!selected || missing.length > 0}>
              插入到输入框
            </Button>
          </span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PromptRow({
  prompt,
  active,
  onSelect,
}: {
  prompt: PromptTemplate;
  active: boolean;
  onSelect: () => void;
}) {
  const preset = isPresetPrompt(prompt);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={cn(
        "w-full border-b border-border px-4 py-3 text-left transition-colors hover:bg-accent/60",
        active && "bg-accent"
      )}
    >
      <span className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {prompt.title}
        </span>
        <Badge variant={preset ? "secondary" : "outline"} className="shrink-0 text-[10px]">
          {preset ? "预置" : "我的"}
        </Badge>
      </span>
      <span className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className="truncate">{prompt.category}</span>
        <span aria-hidden>·</span>
        <span className="shrink-0">{relativeTime(prompt.updated_at)}</span>
      </span>
    </button>
  );
}

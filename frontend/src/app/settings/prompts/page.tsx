"use client";

/** 设置 → 提示词库：管自己的模板（新建 / 改 / 删）。
 *
 * 这里刻意只列 ``scope=mine``：平台预置模板对所有人可读、只有管理员能写（服务端用
 * 403 兜底），放进个人管理页就是个改不动的按钮。要在预置的基础上改改，走对话里的
 * 「提示词库」选择器 —— 它可以「另存为我的模板」。
 *
 * 表单与校验用 `@/lib/prompt-apply` + 共享的 `PromptTemplateForm`：设置页存得进去的
 * 模板，在选择弹窗里也一定填得进去，规则不必两边各抄一份。
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { LibraryBig, Pencil, Plus, Search, Trash2 } from "lucide-react";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import {
  promptCreateBody,
  promptFormFromTemplate,
  promptPatchBody,
  searchFilter,
  type PromptForm,
} from "@/lib/prompt-apply";
import type { PromptTemplate, PromptTemplateInput } from "@/lib/types";
import { relativeTime } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  PromptTemplateForm,
  usePromptForm,
} from "@/components/chat/prompt-library-form";

/** 服务端分页（`app/api/prompts.py` 的 MAX_PAGE_SIZE 是 200），页内再本地筛。 */
const PAGE_SIZE = 100;
/** 与选择弹窗同一套键前缀（`prompt-apply.ts` 的 PROMPTS_QUERY_KEY）：任何写入都把两处一起刷新。 */
const PROMPTS_KEY = ["prompts"] as const;
const CATEGORIES_KEY = ["prompt-categories"] as const;

type SaveArgs =
  | { mode: "create"; body: PromptTemplateInput }
  | { mode: "update"; id: string; patch: Partial<PromptTemplateInput> };

export default function PromptLibrarySettingsPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState("");
  const [category, setCategory] = useState("");
  const [editing, setEditing] = useState<PromptTemplate | null>(null);
  const [creating, setCreating] = useState(false);
  const [morePages, setMorePages] = useState<PromptTemplate[][]>([]);
  const [loadingMore, setLoadingMore] = useState(false);

  const { data: firstPage, isLoading } = useQuery({
    queryKey: [...PROMPTS_KEY, "mine"],
    queryFn: () => api.listPrompts({ scope: "mine", limit: PAGE_SIZE }),
  });
  const { data: categories = [] } = useQuery({
    queryKey: [...CATEGORIES_KEY, "mine"],
    queryFn: () => api.listPromptCategories("mine"),
  });

  // 去重后的并集：别处（对话里的选择器）新建后 refetch 会与本地追加的页重叠，
  // 不去重就会看到同一张模板出现两次。
  const all = useMemo(() => {
    const seen = new Set<string>();
    const rows: PromptTemplate[] = [];
    for (const row of [...(firstPage ?? []), ...morePages.flat()]) {
      if (!seen.has(row.id)) {
        seen.add(row.id);
        rows.push(row);
      }
    }
    return rows;
  }, [firstPage, morePages]);

  const fetchedRows =
    (firstPage?.length ?? 0) + morePages.reduce((n, page) => n + page.length, 0);
  const lastPage =
    morePages.length > 0 ? morePages[morePages.length - 1] : (firstPage ?? []);
  const hasMore = lastPage.length >= PAGE_SIZE;

  const visible = useMemo(
    () => searchFilter(all, { q: filter, category, scope: "mine" }),
    [all, filter, category],
  );

  const invalidate = () => {
    // 整片失效：选择弹窗按 scope/category 分了子键，只刷 "mine" 那一条会让它拿着
    // 过期数据挑模板。
    qc.invalidateQueries({ queryKey: PROMPTS_KEY });
    qc.invalidateQueries({ queryKey: CATEGORIES_KEY });
  };

  const saveMutation = useMutation({
    mutationFn: (args: SaveArgs) =>
      args.mode === "create"
        ? api.createPrompt(args.body)
        : api.updatePrompt(args.id, args.patch),
    onSuccess: (row) => {
      toast.success(`模板「${row.title}」已保存`);
      invalidate();
      closeEditor();
    },
    onError: (err: unknown) =>
      toast.error("保存失败", { description: userErrorMessage(err) }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deletePrompt(id),
    onSuccess: () => {
      toast.success("模板已删除");
      invalidate();
    },
    onError: (err: unknown) =>
      toast.error("删除失败", { description: userErrorMessage(err) }),
  });

  function closeEditor() {
    setEditing(null);
    setCreating(false);
  }

  /**
   * 新建发全量 POST，编辑只发 diff 出来的 PATCH。
   *
   * 一个字段都没改时直接关窗：服务端对空 PATCH 回 400（「没有需要更新的字段」），
   * 把那个 400 弹成「保存失败」是拿用户当调试器用。
   */
  function handleSubmit(form: PromptForm) {
    if (!editing) {
      saveMutation.mutate({ mode: "create", body: promptCreateBody(form) });
      return;
    }
    const patch = promptPatchBody(form, editing);
    if (!patch) {
      closeEditor();
      return;
    }
    saveMutation.mutate({ mode: "update", id: editing.id, patch });
  }

  function handleDelete(row: PromptTemplate) {
    if (!window.confirm(`删除模板「${row.title}」？此操作不可恢复。`)) return;
    deleteMutation.mutate(row.id);
  }

  async function handleLoadMore() {
    setLoadingMore(true);
    try {
      const next = await api.listPrompts({
        scope: "mine",
        limit: PAGE_SIZE,
        offset: fetchedRows,
      });
      setMorePages((pages) => [...pages, next]);
    } catch (err: unknown) {
      toast.error("加载失败", { description: userErrorMessage(err) });
    } finally {
      setLoadingMore(false);
    }
  }

  const editorOpen = creating || editing !== null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold">提示词库</h1>
          <p className="text-sm text-muted-foreground">
            把反复使用的提示词存成模板，在对话里一键插入。平台预置模板由运营维护，
            在对话的「提示词库」中选择，或另存为自己的模板后再改。
          </p>
        </div>
        <Button onClick={() => setCreating(true)} className="gap-2">
          <Plus className="h-4 w-4" /> 新建模板
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 max-w-sm flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="搜索标题、正文或标签…"
            aria-label="搜索我的模板"
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <CategoryChip label="全部分类" active={category === ""} onClick={() => setCategory("")} />
          {categories.map((name) => (
            <CategoryChip
              key={name}
              label={name}
              active={category === name}
              onClick={() => setCategory(category === name ? "" : name)}
            />
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      ) : visible.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-8 text-center">
          <LibraryBig className="mx-auto h-6 w-6 text-muted-foreground/60" />
          <p className="mt-2 text-sm text-muted-foreground">
            {all.length === 0
              ? "还没有自己的模板。新建一个，或在对话的「提示词库」里挑个预置模板另存。"
              : "没有匹配的模板，换个关键词或分类试试。"}
          </p>
        </div>
      ) : (
        <div className="grid gap-3">
          {visible.map((row) => (
            <div
              key={row.id}
              className="flex flex-wrap items-start gap-3 rounded-lg border border-border bg-card p-4"
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium">{row.title}</span>
                  <Badge variant="outline">{row.category}</Badge>
                  {(row.tags ?? []).map((tag) => (
                    <Badge key={tag} variant="secondary" className="text-[10px]">
                      {tag}
                    </Badge>
                  ))}
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                  {row.description || row.content}
                </p>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  更新于 {relativeTime(row.updated_at)}
                </p>
              </div>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setCreating(false);
                    setEditing(row);
                  }}
                  title="编辑"
                  aria-label={`编辑 ${row.title}`}
                >
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => handleDelete(row)}
                  disabled={deleteMutation.isPending}
                  title="删除"
                  aria-label={`删除 ${row.title}`}
                >
                  <Trash2 className="h-3.5 w-3.5 text-destructive" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {hasMore && !isLoading ? (
        <div className="flex justify-center">
          <Button
            variant="outline"
            size="sm"
            onClick={handleLoadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "加载中…" : "加载更多"}
          </Button>
        </div>
      ) : null}

      <Dialog
        open={editorOpen}
        onOpenChange={(open) => {
          if (!open) closeEditor();
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑模板" : "新建模板"}</DialogTitle>
            <DialogDescription>
              正文里 {"{{变量}}"} 与 {"${变量}"} 都算占位符，插入到输入框时会先让你填好。
            </DialogDescription>
          </DialogHeader>
          {editorOpen ? (
            <Editor
              // key 让「切换编辑对象」必然重挂：初值只在挂载时读一次，不必用 effect
              // 回滚，也不会出现「先看到上一个模板的一帧」。
              key={editing?.id ?? "create"}
              initial={promptFormFromTemplate(editing)}
              categoryOptions={categories}
              submitting={saveMutation.isPending}
              onCancel={closeEditor}
              onSubmit={handleSubmit}
            />
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Editor({
  initial,
  categoryOptions,
  submitting,
  onCancel,
  onSubmit,
}: {
  initial: PromptForm;
  categoryOptions: string[];
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (form: PromptForm) => void;
}) {
  const { form, setField, errors, hasErrors } = usePromptForm(initial);
  return (
    <PromptTemplateForm
      form={form}
      errors={errors}
      onFieldChange={setField}
      categoryOptions={categoryOptions}
      submitting={submitting}
      onCancel={onCancel}
      onSubmit={() => {
        if (hasErrors) {
          // 服务端也有一套同样的校验，但把 422 弹给用户之前先在这里拦住更快。
          toast.error(Object.values(errors)[0] ?? "请先修正表单");
          return;
        }
        onSubmit(form);
      }}
    />
  );
}

function CategoryChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        active
          ? "rounded-md bg-secondary px-2.5 py-1 text-xs font-medium text-foreground"
          : "rounded-md px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
      }
    >
      {label}
    </button>
  );
}

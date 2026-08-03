"use client";

import { useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FileText, Layers, Plus, Trash2, Search } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import type { KnowledgeBase } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";
import { resolveChatHome, withReturnTo } from "@/lib/navigation";
import { NavSuspense } from "@/components/navigation/page-loading";
import { AppPageShell } from "@/components/navigation/app-page-shell";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export default function KnowledgeBasesPage() {
  return (
    <NavSuspense>
      <KnowledgeBasesContent />
    </NavSuspense>
  );
}

function KnowledgeBasesContent() {
  const searchParams = useSearchParams();
  // Forwarded to each detail link so "返回对话" survives list → detail → back.
  // Always a clean chat-home target (preserves the conversation id), never a
  // garbage/non-existent path.
  const returnTo = resolveChatHome(searchParams);

  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [embeddingModelId, setEmbeddingModelId] = useState<string>("");

  const { data: kbs, isLoading } = useQuery({
    queryKey: ["knowledge-bases"],
    queryFn: () => api.listKnowledgeBases(),
  });

  // Embedding-capable models populate the embedding-model select.
  const { data: models } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.listModels(),
  });
  const embeddingModels = (models ?? []).filter((m) => m.is_embedding);

  const createMutation = useMutation({
    mutationFn: () =>
      api.createKnowledgeBase({
        name: name.trim(),
        description: description.trim() || undefined,
        embedding_model_id: embeddingModelId || null,
      }),
    onSuccess: (kb) => {
      toast.success(`知识库「${kb.name}」已创建`);
      qc.setQueryData<KnowledgeBase[]>(["knowledge-bases"], (old) =>
        old ? [kb, ...old] : [kb]
      );
      resetForm();
      setOpen(false);
    },
    onError: (err: unknown) => {
      toast.error(err instanceof ApiError ? err.message : "创建失败");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteKnowledgeBase(id),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: ["knowledge-bases"] });
      const prev = qc.getQueryData<KnowledgeBase[]>(["knowledge-bases"]);
      qc.setQueryData<KnowledgeBase[]>(["knowledge-bases"], (old) =>
        (old ?? []).filter((k) => k.id !== id)
      );
      return { prev };
    },
    onError: (err: unknown, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(["knowledge-bases"], ctx.prev);
      toast.error(err instanceof ApiError ? err.message : "删除失败");
    },
    onSuccess: () => toast.success("知识库已删除"),
  });

  function resetForm() {
    setName("");
    setDescription("");
    setEmbeddingModelId("");
  }

  function handleCreate() {
    if (!name.trim()) {
      toast.error("请输入知识库名称");
      return;
    }
    createMutation.mutate();
  }

  function handleDelete(kb: KnowledgeBase) {
    if (!window.confirm(`确定删除知识库「${kb.name}」？此操作不可恢复。`)) return;
    deleteMutation.mutate(kb.id);
  }

  const filtered = (kbs ?? []).filter((k) =>
    k.name.toLowerCase().includes(filter.trim().toLowerCase())
  );

  const createDialog = (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="h-4 w-4" />
          新建知识库
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建知识库</DialogTitle>
          <DialogDescription>
            创建一个知识库，随后上传文档即可启用检索增强。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-2">
          <div className="grid gap-2">
            <Label htmlFor="kb-name">名称</Label>
            <Input
              id="kb-name"
              placeholder="例如：产品手册"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="kb-desc">描述（可选）</Label>
            <Textarea
              id="kb-desc"
              placeholder="简短描述该知识库的用途"
              value={description}
              rows={3}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label>向量模型</Label>
            <Select value={embeddingModelId} onValueChange={setEmbeddingModelId}>
              <SelectTrigger>
                <SelectValue placeholder="选择用于生成向量的模型" />
              </SelectTrigger>
              <SelectContent>
                {embeddingModels.length === 0 ? (
                  <div className="px-2 py-4 text-center text-sm text-muted-foreground">
                    暂无可用的向量模型
                  </div>
                ) : (
                  embeddingModels.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.name}{" "}
                      <span className="text-muted-foreground">({m.model_name})</span>
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </div>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              resetForm();
              setOpen(false);
            }}
          >
            取消
          </Button>
          <Button onClick={handleCreate} disabled={createMutation.isPending}>
            {createMutation.isPending ? "创建中…" : "创建"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return (
    <AppPageShell
      title="知识库"
      description="管理文档集合，为对话提供检索增强（RAG）。"
      actions={createDialog}
    >
      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="搜索知识库…"
          className="pl-9"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="搜索知识库"
        />
      </div>

      {/* List */}
      <div>
        {isLoading ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-40 w-full" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState hasAny={(kbs ?? []).length > 0} />
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {filtered.map((kb) => (
              <KbCard key={kb.id} kb={kb} returnTo={returnTo} onDelete={() => handleDelete(kb)} />
            ))}
          </div>
        )}
      </div>
    </AppPageShell>
  );
}

function KbCard({
  kb,
  returnTo,
  onDelete,
}: {
  kb: KnowledgeBase;
  returnTo: string;
  onDelete: () => void;
}) {
  return (
    <Card className="group relative flex flex-col p-5 transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between gap-3">
        <Link
          href={withReturnTo(`/knowledge-bases/${kb.id}`, returnTo)}
          className="min-w-0 flex-1"
          aria-label={`打开知识库 ${kb.name}`}
        >
          <h3 className="truncate text-base font-semibold hover:underline">{kb.name}</h3>
        </Link>
        <Button
          size="icon"
          variant="ghost"
          className="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
          onClick={onDelete}
          aria-label={`删除知识库 ${kb.name}`}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>

      {kb.description ? (
        <p className="mt-2 line-clamp-2 min-h-[2.5rem] text-sm text-muted-foreground">
          {kb.description}
        </p>
      ) : (
        <p className="mt-2 min-h-[2.5rem] text-sm italic text-muted-foreground/60">暂无描述</p>
      )}

      <div className="mt-4 flex items-center gap-4 text-sm text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <FileText className="h-4 w-4" />
          {kb.document_count} 文档
        </span>
        <span className="flex items-center gap-1.5">
          <Layers className="h-4 w-4" />
          {kb.chunk_count} 分块
        </span>
      </div>

      <div className="mt-3 flex items-center justify-between">
        {kb.embedding_model_id ? (
          <Badge variant="secondary" className="font-normal">
            embedding
          </Badge>
        ) : (
          <span />
        )}
        <span className="text-xs text-muted-foreground">{relativeTime(kb.created_at)}</span>
      </div>
    </Card>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed py-20 text-center"
      )}
    >
      <FileText className="h-10 w-10 text-muted-foreground/50" />
      <p className="mt-4 text-sm font-medium">{hasAny ? "没有匹配的知识库" : "还没有知识库"}</p>
      <p className="mt-1 text-sm text-muted-foreground">
        {hasAny ? "尝试调整搜索关键词。" : "点击右上角「新建知识库」开始上传文档。"}
      </p>
    </div>
  );
}

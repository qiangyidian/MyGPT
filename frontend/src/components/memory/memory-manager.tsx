"use client";

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Brain, Loader2, Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react";

import { memoriesApi } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import {
  DEFAULT_USER_MEMORY_PROPOSE,
  USER_MEMORIES_QUERY_KEY,
  userMemoryIsActive,
} from "@/lib/memories";
import type { UserMemory, UserMemoryProposeInput } from "@/lib/types";
import { useUserMemories } from "@/hooks/useUserMemories";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

const MEMORY_TYPE_LABELS: Record<string, string> = {
  fact: "个人信息",
  preference: "回答偏好",
  summary: "背景摘要",
  instruction: "长期要求",
  task: "持续事项",
};

function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_LABELS[type] ?? "其他";
}

export function MemoryManager({
  conversationId,
  compact = false,
}: {
  /** New memories created from chat retain their conversation provenance. */
  conversationId?: string | null;
  compact?: boolean;
}) {
  const queryClient = useQueryClient();
  const { data: memories = [], isLoading, isError, refetch } = useUserMemories();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<UserMemory | null>(null);
  const [form, setForm] = useState<UserMemoryProposeInput>(DEFAULT_USER_MEMORY_PROPOSE(""));

  const activeCount = useMemo(
    () => memories.filter(userMemoryIsActive).length,
    [memories],
  );
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: USER_MEMORIES_QUERY_KEY });

  const proposeMutation = useMutation({
    mutationFn: (body: UserMemoryProposeInput) => memoriesApi.propose(body),
    onSuccess: () => {
      toast.success("已保存为候选记忆；启用后才会影响回答");
      invalidate();
      setEditorOpen(false);
    },
    onError: (error: unknown) => toast.error("保存失败", { description: userErrorMessage(error) }),
  });
  const editMutation = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      memoriesApi.edit(id, { content }),
    onSuccess: () => {
      toast.success("记忆已更新");
      invalidate();
      setEditorOpen(false);
    },
    onError: (error: unknown) => toast.error("保存失败", { description: userErrorMessage(error) }),
  });
  const toggleMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      active ? memoriesApi.activate(id) : memoriesApi.deactivate(id),
    onSuccess: (_memory, variables) => {
      toast.success(variables.active ? "记忆已启用，将用于后续相关回答" : "记忆已停用");
      invalidate();
    },
    onError: (error: unknown) => toast.error("记忆状态更新失败", { description: userErrorMessage(error) }),
  });
  const disableAllMutation = useMutation({
    mutationFn: () => memoriesApi.bulkSet(false),
    onSuccess: () => {
      toast.success("已停用全部记忆；记录仍保留，可随时重新启用");
      invalidate();
    },
    onError: (error: unknown) => toast.error("停用失败", { description: userErrorMessage(error) }),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => memoriesApi.delete(id),
    onSuccess: () => {
      toast.success("记忆已删除");
      invalidate();
    },
    onError: (error: unknown) => toast.error("删除失败", { description: userErrorMessage(error) }),
  });

  function openCreate() {
    setEditing(null);
    setForm({
      ...DEFAULT_USER_MEMORY_PROPOSE(""),
      ...(conversationId ? { source_conversation_id: conversationId } : {}),
    });
    setEditorOpen(true);
  }

  function openEdit(memory: UserMemory) {
    setEditing(memory);
    setForm({ ...DEFAULT_USER_MEMORY_PROPOSE(memory.content), memory_type: memory.memory_type });
    setEditorOpen(true);
  }

  function submit() {
    const content = form.content.trim();
    if (!content) {
      toast.error("请填写记忆内容");
      return;
    }
    if (editing) {
      editMutation.mutate({ id: editing.id, content });
      return;
    }
    proposeMutation.mutate({ ...form, content, active: false });
  }

  const busy = toggleMutation.isPending || deleteMutation.isPending;

  return (
    <section className={compact ? "space-y-4" : "space-y-6"}>
      <header className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
        <div className="space-y-1">
          {!compact && <h1 className="text-2xl font-semibold tracking-tight">长期记忆</h1>}
          <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
            只有你启用的记忆才会进入后续回答。自动发现的内容会先作为候选项保存；你可以查看、修改、停用或删除。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => disableAllMutation.mutate()}
            disabled={activeCount === 0 || disableAllMutation.isPending}
          >
            {disableAllMutation.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            全部停用
          </Button>
          <Button size="sm" onClick={openCreate}>
            <Plus className="mr-1.5 h-4 w-4" /> 新增记忆
          </Button>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-border bg-muted/30 px-4 py-3">
        <span className="inline-flex items-center gap-2 text-sm font-medium">
          <ShieldCheck className="h-4 w-4 text-primary" />
          {activeCount} 条记忆已启用
        </span>
        <span className="text-xs text-muted-foreground">
          启用表示同意这些内容用于后续相关对话；停用会立即停止注入，但保留记录。
        </span>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground" role="status">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在读取记忆…
        </div>
      ) : isError ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-5 text-sm">
          <p className="font-medium">记忆暂时无法加载</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void refetch()}>
            重试
          </Button>
        </div>
      ) : memories.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border px-5 py-8 text-center">
          <Brain className="mx-auto h-7 w-7 text-muted-foreground/70" />
          <p className="mt-3 text-sm font-medium">还没有长期记忆</p>
          <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-muted-foreground">
            对话中发现的个人信息和偏好会先作为候选项显示在这里，不会自动用于回答。你也可以手动添加。
          </p>
        </div>
      ) : (
        <div className="grid gap-2">
          {memories.map((memory) => {
            const active = userMemoryIsActive(memory);
            const expired = memory.active && !active;
            return (
              <article
                key={memory.id}
                className="group rounded-xl border border-border bg-card p-3 transition-colors hover:border-border/70 hover:bg-muted/20 sm:p-4"
              >
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Brain className="h-4 w-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant="outline" className="text-[10px] font-medium">
                        {memoryTypeLabel(memory.memory_type)}
                      </Badge>
                      <Badge
                        variant="outline"
                        className={active ? "border-emerald-600/30 text-emerald-700 dark:text-emerald-400" : "text-muted-foreground"}
                      >
                        {active ? "用于相关回答" : expired ? "已过期" : "候选 · 未启用"}
                      </Badge>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed">
                      {memory.content}
                    </p>
                    <p className="mt-1.5 text-[11px] text-muted-foreground">
                      {memory.source_message_id ? "来自对话" : "手动添加"}
                      {memory.expires_at && ` · ${new Date(memory.expires_at).toLocaleDateString()} 过期`}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Switch
                      checked={active}
                      onCheckedChange={(next) => toggleMutation.mutate({ id: memory.id, active: next })}
                      disabled={busy}
                      aria-label={`${active ? "停用" : "启用"}记忆：${memory.content}`}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8"
                      onClick={() => openEdit(memory)}
                      aria-label={`编辑记忆：${memory.content}`}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 text-muted-foreground hover:text-destructive"
                      onClick={() => {
                        if (window.confirm("删除这条记忆？之后无法恢复。")) deleteMutation.mutate(memory.id);
                      }}
                      disabled={busy}
                      aria-label={`删除记忆：${memory.content}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑记忆" : "新增候选记忆"}</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-1.5">
              <Label htmlFor="memory-content">记忆内容</Label>
              <Textarea
                id="memory-content"
                autoFocus
                value={form.content}
                onChange={(event) => setForm((current) => ({ ...current, content: event.target.value }))}
                className="min-h-24 resize-y"
                maxLength={2000}
                placeholder="例如：我更喜欢简洁的回答，代码示例优先使用 Python。"
              />
            </div>
            {!editing && (
              <div className="grid gap-1.5">
                <Label htmlFor="memory-type">类型</Label>
                <Select
                  value={form.memory_type ?? "fact"}
                  onValueChange={(value) => setForm((current) => ({ ...current, memory_type: value }))}
                >
                  <SelectTrigger id="memory-type"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {Object.entries(MEMORY_TYPE_LABELS).map(([value, label]) => (
                      <SelectItem key={value} value={value}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            {!editing && (
              <p className="text-xs leading-relaxed text-muted-foreground">
                新记忆会先保存为未启用候选项。保存后可在列表中单独启用，避免未经确认的内容影响回答。
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditorOpen(false)}>取消</Button>
            <Button
              onClick={submit}
              disabled={proposeMutation.isPending || editMutation.isPending || !form.content.trim()}
            >
              {(proposeMutation.isPending || editMutation.isPending) && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />}
              {editing ? "保存修改" : "保存为候选"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

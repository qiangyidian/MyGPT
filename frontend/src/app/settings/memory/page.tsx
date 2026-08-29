"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Brain, Pencil, Plus, Trash2 } from "lucide-react";

import { memoriesApi, ApiError } from "@/lib/api";
import { DEFAULT_USER_MEMORY_PROPOSE, userMemoryIsActive } from "@/lib/memories";
import type { UserMemory, UserMemoryProposeInput } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
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

const QUERY_KEY = ["user-memories"] as const;

export default function MemoryPage() {
  const qc = useQueryClient();
  const { data: memories = [], isLoading } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: memoriesApi.list,
  });

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<UserMemory | null>(null);
  const [form, setForm] = useState<UserMemoryProposeInput>(
    DEFAULT_USER_MEMORY_PROPOSE(""),
  );

  const invalidate = () => qc.invalidateQueries({ queryKey: QUERY_KEY });

  const activeCount = useMemo(
    () => memories.filter(userMemoryIsActive).length,
    [memories],
  );

  const proposeMut = useMutation({
    mutationFn: (body: UserMemoryProposeInput) => memoriesApi.propose(body),
    onSuccess: () => {
      toast.success("已记录候选记忆（默认未启用）");
      invalidate();
      setOpen(false);
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const editMut = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      memoriesApi.edit(id, { content }),
    onSuccess: () => {
      toast.success("已保存");
      invalidate();
      setOpen(false);
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const toggleMut = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      active ? memoriesApi.activate(id) : memoriesApi.deactivate(id),
    onSuccess: () => invalidate(),
    onError: (e: ApiError) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => memoriesApi.delete(id),
    onSuccess: () => {
      toast.success("已删除");
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const disableAllMut = useMutation({
    mutationFn: () => memoriesApi.bulkSet(false),
    onSuccess: (res) => {
      toast.success(`已停用 ${res.deactivated ?? 0} 条记忆`);
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  function openCreate() {
    setEditing(null);
    setForm(DEFAULT_USER_MEMORY_PROPOSE(""));
    setOpen(true);
  }

  function openEdit(m: UserMemory) {
    setEditing(m);
    setForm({ content: m.content, memory_type: m.memory_type, active: false });
    setOpen(true);
  }

  function submit() {
    if (!form.content.trim()) {
      toast.error("请填写内容");
      return;
    }
    if (editing) editMut.mutate({ id: editing.id, content: form.content.trim() });
    else proposeMut.mutate({ ...form, content: form.content.trim() });
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">长期记忆</h1>
          <p className="text-sm text-muted-foreground">
            可选开启：跨会话的语义记忆。新记忆默认
            <strong>未启用</strong>
            ，需手动开启后才会进入对话上下文。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => disableAllMut.mutate()}
            disabled={disableAllMut.isPending || activeCount === 0}
            className="gap-2"
            title="停用全部记忆（不删除数据）"
          >
            全部停用
          </Button>
          <Button onClick={openCreate} className="gap-2">
            <Plus className="h-4 w-4" /> 新增记忆
          </Button>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground">
        当前生效记忆：<span className="font-medium text-foreground">{activeCount}</span> / {memories.length} 条
      </div>

      {isLoading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : memories.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          还没有记忆。对话中会自动从你的消息提取候选记忆（默认未启用，需在此开启），
          也可手动新增。
        </p>
      ) : (
        <div className="grid gap-2">
          {memories.map((m) => {
            const active = userMemoryIsActive(m);
            return (
              <div
                key={m.id}
                className="flex flex-wrap items-start gap-3 rounded-lg border border-border bg-card p-3"
              >
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-secondary">
                  <Brain className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="text-[10px]">
                      {m.memory_type}
                    </Badge>
                    {active ? (
                      <Badge variant="outline" className="text-[10px] text-green-600">
                        已启用
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="text-[10px] text-muted-foreground">
                        未启用
                      </Badge>
                    )}
                    <span className="text-[10px] text-muted-foreground">
                      置信度 {(m.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  <p className="mt-1 text-sm text-foreground">{m.content}</p>
                  {m.expires_at && (
                    <p className="mt-0.5 text-[10px] text-muted-foreground">
                      过期：{new Date(m.expires_at).toLocaleString()}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    checked={active}
                    onCheckedChange={(v) =>
                      toggleMut.mutate({ id: m.id, active: v })
                    }
                    disabled={toggleMut.isPending}
                    aria-label={active ? "停用" : "启用"}
                  />
                  <Button variant="ghost" size="sm" onClick={() => openEdit(m)}>
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      if (confirm("删除这条记忆？")) deleteMut.mutate(m.id);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5 text-destructive" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑记忆" : "新增候选记忆"}</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-1.5">
              <Label className="text-xs text-muted-foreground">内容</Label>
              <Textarea
                autoFocus
                value={form.content}
                onChange={(e) => setForm({ ...form, content: e.target.value })}
                className="min-h-[80px]"
                placeholder="例如：用户偏好简洁回答、使用 Python 等"
              />
            </div>
            {!editing && (
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-1.5">
                  <Label className="text-xs text-muted-foreground">类型</Label>
                  <Select
                    value={form.memory_type ?? "fact"}
                    onValueChange={(v) =>
                      setForm({ ...form, memory_type: v })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="fact">fact</SelectItem>
                      <SelectItem value="preference">preference</SelectItem>
                      <SelectItem value="summary">summary</SelectItem>
                      <SelectItem value="task">task</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    置信度（0–1）
                  </Label>
                  <Input
                    type="number"
                    step="0.1"
                    min="0"
                    max="1"
                    value={String(form.confidence ?? 0.5)}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        confidence: Number(e.target.value) || 0,
                      })
                    }
                  />
                </div>
              </div>
            )}
            {!editing && (
              <p className="text-[11px] text-muted-foreground">
                新记忆默认<strong>未启用</strong>，保存后可在列表中开启。
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button
              onClick={submit}
              disabled={proposeMut.isPending || editMut.isPending}
            >
              {editing ? "保存" : "新增"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

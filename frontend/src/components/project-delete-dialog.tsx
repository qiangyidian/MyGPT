"use client";

import { useState } from "react";
import { AlertTriangle, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { userErrorMessage } from "@/lib/api-error";
import type { Project } from "@/lib/types";
import { useProjectImpact } from "@/hooks/useProjects";
import { projectDeleteConsequences, projectDeleteConfirmed } from "@/lib/projects";

interface ProjectDeleteDialogProps {
  open: boolean;
  project: Project | null;
  onOpenChange: (open: boolean) => void;
  /** Calls `DELETE /api/projects/{id}`; rejects with an ApiError (Chinese message). */
  onDelete: (projectId: string) => Promise<void>;
}

/**
 * 删除项目（条目 30）。
 *
 * 确认文案里的每个数字都来自 `GET /api/projects/{id}/impact`，不在前端估：
 * 「此操作不可撤销」如果连带一句真实的后果清单都没有，用户既学不到项目与会话
 * 的关系，也无从判断自己按下了什么。影响范围没读到时不允许删除 —— 一个不知
 * 道会带走什么的确认框，比没有确认框更糟。
 */
export function ProjectDeleteDialog({
  open,
  project,
  onOpenChange,
  onDelete,
}: ProjectDeleteDialogProps) {
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);
  const impact = useProjectImpact(open ? (project?.id ?? null) : null);

  if (!project) return null;

  const consequences = projectDeleteConsequences(project, impact.data);
  const ready = !impact.isPending && !impact.isError;
  const confirmed = projectDeleteConfirmed(project.name, typed);

  const submit = async () => {
    if (!ready || !confirmed || pending) return;
    setPending(true);
    try {
      await onDelete(project.id);
      onOpenChange(false);
      setTyped("");
      toast.success("项目已删除", {
        description: impact.data?.deletes_conversations
          ? undefined
          : "会话已保留，改为未分组。",
      });
    } catch (err) {
      toast.error("删除失败", { description: userErrorMessage(err) });
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <Trash2 className="h-4 w-4" />
            删除项目「{project.name}」
          </DialogTitle>
          <DialogDescription>
            {consequences.destructive
              ? "此操作不可撤销，且会连带删除下面的内容。"
              : "此操作不可撤销，但删除范围仅限项目本身。"}
          </DialogDescription>
        </DialogHeader>

        {impact.isPending && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在统计影响范围……
          </p>
        )}
        {impact.isError && (
          <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3">
            <p className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              无法确认影响范围，暂时不能删除。
            </p>
            <Button
              variant="outline"
              size="sm"
              className="gap-2"
              onClick={() => void impact.refetch()}
            >
              <RefreshCw className="h-4 w-4" />
              重试
            </Button>
          </div>
        )}
        {ready && (
          <ul className="space-y-1.5 text-sm">
            {consequences.lines.map((line) => (
              <li key={line} className="flex gap-2">
                <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-current opacity-60" />
                <span>{line}</span>
              </li>
            ))}
          </ul>
        )}
        {ready && consequences.destructive && (
          <p className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-xs text-destructive">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            后端会连同会话与消息一起删除，删除后无法恢复。
          </p>
        )}

        <div className="space-y-1.5">
          <label className="text-xs text-muted-foreground" htmlFor="project-delete-confirm">
            请输入项目名称 <span className="font-medium text-foreground">{project.name}</span> 以确认
          </label>
          <Input
            id="project-delete-confirm"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            disabled={!ready}
            autoComplete="off"
            placeholder={project.name}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={pending}>
            取消
          </Button>
          <Button
            variant="destructive"
            onClick={() => void submit()}
            disabled={!ready || !confirmed || pending}
          >
            {pending && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
            删除项目
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

"use client";

// 后台「工具」Tab（条目 34③）：目录 + 启停。
//
// 这一页原先是一份只读卡片墙 —— 运营看得见某个工具有多危险，却没有任何手段在出事
// 之后把它停掉，唯一的开关是改 `.env` 再发一次版，而"要不要停"恰恰是最需要当场决定
// 的那一刻。现在状态存在 `tool_toggles` 表里，跨进程生效（见
// `backend/app/services/tool_toggles.py`）。

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import {
  catalogSummary,
  isToolEnabled,
  sortCatalog,
  TOGGLE_EFFECT_NOTE,
  toggleResultMessage,
} from "@/lib/tool-catalog";
import type { ToolInfo } from "@/lib/types";

const TOOLS_QUERY_KEY = ["admin-tools"] as const;

export function ToolCatalogPanel() {
  const qc = useQueryClient();
  // 一次只改一个开关：并发改两个工具时，两个请求都还在飞，界面上却已经显示成
  // 用户点出来的样子 —— 那时失败的那一条会被读成"已经关了"。
  const [busyName, setBusyName] = useState<string | null>(null);
  const [disableTarget, setDisableTarget] = useState<ToolInfo | null>(null);
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | null>(null);

  const toolsQ = useQuery({
    queryKey: TOOLS_QUERY_KEY,
    // 后台这一份含已被停用的工具；换成用户侧的 api.listTools() 会得到一份"关掉之后就
    // 再也找不回来"的目录。
    queryFn: () => api.adminListTools(),
  });

  const toggle = useMutation({
    mutationFn: ({ name, enabled, note }: { name: string; enabled: boolean; note?: string }) =>
      api.adminSetToolEnabled(name, enabled, note),
    onSuccess: (tool) => {
      toast.success(toggleResultMessage(tool.name, tool.enabled === true), {
        description: TOGGLE_EFFECT_NOTE,
      });
      qc.invalidateQueries({ queryKey: TOOLS_QUERY_KEY });
    },
    onError: (err) => {
      toast.error("操作失败", { description: userErrorMessage(err) });
    },
    onSettled: () => {
      setBusyName(null);
    },
  });

  async function runToggle(name: string, enabled: boolean, note?: string) {
    setBusyName(name);
    try {
      await toggle.mutateAsync({ name, enabled, note });
    } catch {
      // 失败已在 onError 里给过 toast；这里吞掉是为了不让对话框的 promise 抛出未处理
      // rejection（按下失败后对话框应当停在原处，让用户能改理由再试一次）。
    }
  }

  function confirmDisable() {
    const target = disableTarget;
    if (!target) return;
    const text = reason.trim();
    // 停用必须留下理由。这不是形式主义：库里那一行的 `note` 是一周之后唯一还能回答
    // "当初为什么关它"的地方，而"重新打开"的风险正比于回答不了这个问题。
    if (!text) {
      setReasonError("请填写停用理由（会写进审计与目录）");
      return;
    }
    setReasonError(null);
    void runToggle(target.name, false, text);
    setDisableTarget(null);
    setReason("");
  }

  const rows = sortCatalog(toolsQ.data ?? []);
  const summary = catalogSummary(rows);

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        共 {summary.total} 个工具
        {summary.disabled ? `，其中 ${summary.disabled} 个已停用` : ""}。
        停用只挡新的一次调用，正在跑的不会被掐断；{TOGGLE_EFFECT_NOTE}。
      </p>

      {toolsQ.isError ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-10 text-center">
          <p className="text-sm text-muted-foreground">
            工具目录加载失败：{userErrorMessage(toolsQ.error)}
          </p>
          <Button variant="outline" size="sm" onClick={() => toolsQ.refetch()}>
            重试
          </Button>
        </div>
      ) : toolsQ.isLoading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : !rows.length ? (
        <p className="text-sm text-muted-foreground">注册表里没有工具。</p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {rows.map((tool) => {
            const enabled = isToolEnabled(tool);
            const busy = busyName === tool.name;
            return (
              <div
                key={tool.name}
                className={
                  enabled
                    ? "rounded-lg border border-border bg-card p-3"
                    : "rounded-lg border border-dashed border-border bg-muted/30 p-3"
                }
              >
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-medium">{tool.name}</span>
                  {tool.dangerous && (
                    <Badge variant="destructive" className="text-[10px]">
                      危险
                    </Badge>
                  )}
                  {!enabled && (
                    <Badge variant="outline" className="text-[10px]">
                      已停用
                    </Badge>
                  )}
                  <span className="ml-auto">
                    <Switch
                      checked={enabled}
                      disabled={busy || toggle.isPending}
                      aria-label={`${tool.name} 启用状态`}
                      onCheckedChange={(next) => {
                        if (next) {
                          void runToggle(tool.name, true);
                          return;
                        }
                        setReason("");
                        setReasonError(null);
                        setDisableTarget(tool);
                      }}
                    />
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                  {tool.description || "（无描述）"}
                </p>
                {!enabled && tool.toggle_note ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    停用理由：{tool.toggle_note}
                  </p>
                ) : null}
                {busy ? (
                  <p className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" /> 提交中…
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      <Dialog
        open={disableTarget !== null}
        onOpenChange={(open) => {
          if (!open) {
            setDisableTarget(null);
            setReasonError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>停用 {disableTarget?.name}</DialogTitle>
            <DialogDescription>
              停用之后模型不会再看到这个工具，已经跑到一半的那一次不受影响。
              {TOGGLE_EFFECT_NOTE}。理由会同时写进审计日志。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="tool-disable-reason">停用理由（必填）</Label>
            <Textarea
              id="tool-disable-reason"
              value={reason}
              rows={3}
              placeholder="例如：上游搜索接口返回越权内容，先停掉排查"
              onChange={(e) => {
                setReason(e.target.value);
                if (e.target.value.trim()) setReasonError(null);
              }}
            />
            {reasonError ? (
              <p className="text-xs text-destructive">{reasonError}</p>
            ) : null}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDisableTarget(null);
                setReason("");
                setReasonError(null);
              }}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={toggle.isPending}
              onClick={confirmDisable}
            >
              {toggle.isPending ? (
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              ) : null}
              确认停用
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

"use client";

// 计划门（plan gate）的人工入口。
//
// 后端把「门」做成两件事：``PLAN_REQUIRE_CONFIRMATION`` 决定代码路径存在与否，
// 而真正让某一轮停下来等确认的是 ``POST /api/agent-runs/{id}/gate`` 写下的持久
// gate 命令（``enabled`` 旗标）。生效点是**下一个计划边界**：本轮还没开跑就在
// 本轮开头，已开跑则在下一次重规划。所以这里的按钮改的是「下一道门」，不是
// 「现在暂停」。
//
// 门的状态是**读回来的**（``AgentRun.gate_armed``），不做本地真值：确认/修改
// 计划时引擎自己清闸但不回写命令队列，只有 API 层按 plan_status 收口后的结果
// 才是可信显示。写失败（终态运行 ok:false、429 限速、404）一律回滚成服务端
// 真相并触发一次刷新 —— 命令队列没有版本号，能做的乐观控制就是「不假装成功」。

import { useState } from "react";
import { Loader2, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { ApiError } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import type { RunActionResult } from "@/lib/types";

const TERMINAL = ["completed", "failed", "cancelled"];

interface PlanGateControlProps {
  runId: string;
  /** 后端算出的当前闸态（最后一条持久 gate 命令 + plan_status 收口）。 */
  armed: boolean;
  /** 运行状态；终态时门不再生效。 */
  runStatus: string;
  /** 计划状态：draft | confirmed | updated —— 决定「等待确认」是否还成立。 */
  planStatus?: string | null;
  /** 写操作：``POST /api/agent-runs/{id}/gate``，由调用方注入以便复用与测试。 */
  onGate: (runId: string, enabled: boolean) => Promise<RunActionResult>;
  /** 冲突后让调用方重新拉取运行详情（乐观回滚的落地动作）。 */
  onDecided?: () => void;
  className?: string;
}

/** 把后端回话翻成面向运营的中文；未知消息原样透出，绝不吞掉原因。 */
function gateMessage(result: RunActionResult, enabled: boolean): string {
  if (result.message) return result.message;
  return enabled ? "计划门已上闸" : "计划门已解除";
}

export function PlanGateControl({
  runId,
  armed,
  runStatus,
  planStatus,
  onGate,
  onDecided,
  className,
}: PlanGateControlProps) {
  const [busy, setBusy] = useState<"arm" | "release" | null>(null);
  const terminal = TERMINAL.includes(runStatus);
  const decided = planStatus === "confirmed" || planStatus === "updated";

  const apply = async (enabled: boolean) => {
    setBusy(enabled ? "arm" : "release");
    try {
      const result = await onGate(runId, enabled);
      if (!result.ok) {
        // 后端拒绝（多半是运行已终态）：不假装生效，立刻回读服务端真相。
        toast.warning(gateMessage(result, enabled));
        onDecided?.();
        return;
      }
      toast.success(gateMessage(result, enabled));
      onDecided?.();
    } catch (err) {
      toast.error(gateErrorText(err));
      onDecided?.();
    } finally {
      setBusy(null);
    }
  };

  if (terminal) {
    return (
      <p className={cn("text-[11px] text-muted-foreground", className)}>
        运行已结束，计划门不再生效。
      </p>
    );
  }

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <Button
        size="sm"
        variant={armed ? "outline" : "secondary"}
        className="h-7 gap-1 text-[11px]"
        disabled={busy !== null}
        onClick={() => void apply(!armed)}
      >
        {busy !== null ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : armed ? (
          <ShieldCheck className="h-3 w-3" />
        ) : (
          <ShieldAlert className="h-3 w-3" />
        )}
        {busy === "arm"
          ? "上闸中…"
          : busy === "release"
            ? "撤闸中…"
            : armed
              ? "解除计划门"
              : "计划需我确认"}
      </Button>

      <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
        {armed ? (
          <>
            <ShieldQuestion className="h-3 w-3 text-blue-600 dark:text-blue-400" />
            {decided
              ? "已上闸：下一个计划边界仍会等你确认"
              : "已上闸：计划在下一个计划边界停下等你确认"}
          </>
        ) : (
          "未上闸：计划先行、不阻塞执行"
        )}
      </span>
    </div>
  );
}

/**
 * 错误文案：429 是 ``rate_limit_user(60, 60, "approval")`` 命中，404 是运行已
 * 不在了 —— 这两种都要给出下一步动作，而不是一个「失败」。
 */
export function gateErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return "操作过于频繁，请 1 分钟后再试";
    if (err.status === 404) return "运行不存在或已清理";
    if (err.status === 401) return "会话已过期，请重新登录";
  }
  // 其余（含服务端英文/内部信息）走统一映射，保证不漏英文。
  return userErrorMessage(err);
}

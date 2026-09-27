"use client";

// 管理端 · Agent 运行时观测。
//
// 用户侧的执行面板（`components/context/execution-tab.tsx`）只看得到自己
// 当前会话的那一次运行；运营要回答的是另一类问题：「昨晚有多少 run 卡在
// 等待确认」「这个 profile 的平均耗时/成本」「这一条为什么失败」。所以这里
// 是一层跨用户的列表 + 单条详情，而不是把用户面板换个皮。
//
// 渲染部分一律复用 `components/agents/*`（coerceGraph + AgentRunHeader /
// AgentFlowGraph / AgentActivityFeed / PlanReview / PlanGateControl）：图快照
// 的形状只有后端一份，抄第二份解析器迟早会和它走偏。

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { formatCreditsRaw } from "@/lib/credits";
import { withConversationParam } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import type {
  AdminAgentRunRow,
  AdminRunCommandRow,
  AdminRunEventRow,
  AdminRunSummary,
} from "@/lib/types";
import { coerceGraph } from "@/hooks/useAgentRunGraph";
import { NavSuspense } from "@/components/navigation/page-loading";
import { AppPageShell } from "@/components/navigation/app-page-shell";
import { AgentRunHeader, PROFILE_LABELS } from "@/components/agents/agent-run-header";
import { AgentFlowGraph } from "@/components/agents/agent-flow-graph";
import { AgentActivityFeed } from "@/components/agents/agent-activity-feed";
import { PlanReview } from "@/components/agents/plan-review";
import { PlanGateControl } from "@/components/agents/plan-gate-control";
import { ApprovalCard } from "@/components/approval-card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

const PAGE_SIZE = 25;
const TERMINAL = ["completed", "failed", "cancelled"];

// 与后端 ``RUN_STATUSES`` 白名单一致（传别的会被 400）。
const STATUS_FILTERS: { value: string | null; label: string }[] = [
  { value: null, label: "全部" },
  { value: "running", label: "执行中" },
  { value: "waiting_approval", label: "等待确认" },
  { value: "pending", label: "准备中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "已失败" },
  { value: "cancelled", label: "已取消" },
];

const FLOW_FILTERS: { value: string | null; label: string }[] = [
  { value: null, label: "全部流程" },
  ...Object.entries(PROFILE_LABELS).map(([value, label]) => ({
    value: value as string | null,
    label,
  })),
];

const RUN_STATUS_TEXT: Record<string, string> = {
  pending: "准备中",
  running: "执行中",
  waiting_approval: "等待确认",
  completed: "已完成",
  failed: "已失败",
  cancelled: "已取消",
};

const COMMAND_TEXT: Record<string, string> = {
  pause: "暂停",
  resume: "恢复",
  cancel: "取消",
  gate: "计划门",
  approve: "工具批准",
  reject: "工具拒绝",
  instruction: "追加指令",
};

const COMMAND_STATUS_TEXT: Record<string, string> = {
  pending: "待消费",
  applied: "已生效",
  consumed: "已生效",
  failed: "失败",
};

/** 毫秒 → 中文时长。列表里读数比原始数字有用，所以宁可不省这一步。 */
function durationText(ms: number | null): string {
  if (ms == null) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
}

function timeText(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/**
 * 运行时观测页。
 *
 * 详情是**就地展开**的一行（不另开路由）：观测场景下人是在扫一批记录，
 * 跳走再回来看不到原来的位置很难用。列表本身按后端分页翻页。
 */
export default function AdminRuntimePage() {
  return (
    <NavSuspense>
      <AppPageShell
        title="运行时观测"
        description="跨用户的 Agent 运行记录：状态、耗时、token / 成本、计划门与事件。"
        requireAdmin
        secondaryBack={{ href: "/admin", label: "返回管理后台" }}
        breadcrumbs={[{ label: "管理后台", href: "/admin" }, { label: "运行时观测" }]}
      >
        <RuntimeContent />
      </AppPageShell>
    </NavSuspense>
  );
}

function RuntimeContent() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<string | null>(null);
  const [flowName, setFlowName] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const runsQ = useQuery({
    queryKey: ["admin-agent-runs", status, flowName, q, page],
    queryFn: () =>
      api.adminListAgentRuns({
        status,
        flowName,
        q: q || null,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
  });
  const summaryQ = useQuery({
    queryKey: ["admin-agent-runs-summary"],
    queryFn: () => api.adminAgentRunsSummary(),
  });

  const items = runsQ.data?.items ?? [];
  const total = runsQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const refresh = () => {
    void runsQ.refetch();
    void summaryQ.refetch();
    qc.invalidateQueries({ queryKey: ["admin-agent-run-commands"] });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          只读观测面。审批与计划门是运营介入运行的两个入口，改动的都是那一行运行本身。
        </p>
        <Button variant="outline" size="sm" onClick={refresh}>
          <RefreshCw className="h-3.5 w-3.5" />
          刷新
        </Button>
      </div>

      <SummaryCards summary={summaryQ.data} />

      {/* 过滤器 */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {STATUS_FILTERS.map((f) => (
            <Button
              key={f.label}
              size="sm"
              variant={status === f.value ? "default" : "outline"}
              onClick={() => {
                setStatus(f.value);
                setPage(0);
              }}
            >
              {f.label}
            </Button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex flex-wrap items-center gap-1.5">
            {FLOW_FILTERS.map((f) => (
              <Button
                key={f.label}
                size="sm"
                variant={flowName === f.value ? "default" : "outline"}
                onClick={() => {
                  setFlowName(f.value);
                  setPage(0);
                }}
              >
                {f.label}
              </Button>
            ))}
          </div>
          <form
            className="ml-auto flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              setQ(search.trim());
              setPage(0);
            }}
          >
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="按用户邮箱 / 用户名搜索"
              className="max-w-56"
            />
            <Button type="submit" size="sm" variant="outline">
              搜索
            </Button>
          </form>
        </div>
      </div>

      {runsQ.isError ? (
        <ErrorState onRetry={() => runsQ.refetch()} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">状态</th>
                <th className="p-3">流程 / 运行时</th>
                <th className="hidden p-3 lg:table-cell">用户</th>
                <th className="hidden p-3 md:table-cell">会话</th>
                <th className="p-3 text-right">Token</th>
                <th className="hidden p-3 text-right md:table-cell">成本</th>
                <th className="hidden p-3 text-right sm:table-cell">耗时</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {runsQ.isLoading ? (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !items.length ? (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-muted-foreground">
                    没有符合条件的运行。
                  </td>
                </tr>
              ) : (
                items.map((row: AdminAgentRunRow) => (
                  <RunRow
                    key={row.id}
                    row={row}
                    open={selected === row.id}
                    onToggle={() =>
                      setSelected((cur) => (cur === row.id ? null : row.id))
                    }
                    onChanged={refresh}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-between">
        <Button
          variant="outline"
          size="sm"
          disabled={page === 0}
          onClick={() => setPage((p) => Math.max(0, p - 1))}
        >
          上一页
        </Button>
        <span className="text-xs text-muted-foreground">
          共 {total} 条 · 第 {Math.min(page + 1, pages)} / {pages} 页
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={(page + 1) * PAGE_SIZE >= total}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

function SummaryCards({ summary }: { summary?: AdminRunSummary }) {
  if (!summary) return null;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <Stat label="近 24 时运行" value={String(summary.total_runs)} />
      <Stat label="执行中" value={String(summary.running)} />
      <Stat label="等待确认" value={String(summary.waiting_approval)} />
      <Stat label="失败" value={String(summary.failed)} />
      <Stat
        label="Token（输入/输出）"
        value={`${summary.prompt_tokens} / ${summary.completion_tokens}`}
      />
      <Stat
        label="成本 / 均耗时"
        value={`$${summary.cost_usd.toFixed(4)} · ${durationText(summary.avg_duration_ms)}`}
      />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-12 text-center">
      <p className="text-sm text-muted-foreground">运行列表加载失败，请重试。</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        重试
      </Button>
    </div>
  );
}

/** 一行运行 + 展开后的详情（详情用同一个 table 里的 colSpan 行塞进去，保持列表位置）。 */
function RunRow({
  row,
  open,
  onToggle,
  onChanged,
}: {
  row: AdminAgentRunRow;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const attention =
    row.status === "waiting_approval" || row.gate_armed || Boolean(row.paused_at);
  return (
    <>
      <tr className={cn("border-t border-border", attention && "bg-amber-500/5")}>
        <td className="p-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{RUN_STATUS_TEXT[row.status] ?? row.status}</span>
            {row.gate_armed ? (
              <Badge variant="outline" className="text-[10px]">
                计划门
              </Badge>
            ) : null}
            {row.paused_at ? (
              <Badge variant="outline" className="text-[10px]">
                已暂停
              </Badge>
            ) : null}
            {row.pending_approvals > 0 ? (
              <Badge variant="destructive" className="text-[10px]">
                {row.pending_approvals} 项待批准
              </Badge>
            ) : null}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {row.current_step || "—"} · {timeText(row.created_at)}
          </div>
        </td>
        <td className="p-3">
          <div>{PROFILE_LABELS[row.flow_name] ?? row.flow_name}</div>
          <div className="text-xs text-muted-foreground">{row.runtime}</div>
        </td>
        <td className="hidden p-3 lg:table-cell">
          <div>{row.user_username ?? "—"}</div>
          <div className="text-xs text-muted-foreground">{row.user_email ?? ""}</div>
        </td>
        <td className="hidden max-w-[200px] p-3 md:table-cell">
          <div className="truncate">{row.conversation_title || "（无标题）"}</div>
          <div className="truncate text-xs text-muted-foreground">{row.conversation_id}</div>
        </td>
        <td className="p-3 text-right tabular-nums">
          {row.total_tokens}
          <div className="text-xs text-muted-foreground">
            {row.step_count} 步
          </div>
        </td>
        <td className="hidden p-3 text-right tabular-nums md:table-cell">
          {row.cost_usd != null ? `$${row.cost_usd.toFixed(4)}` : "—"}
          <div className="text-xs text-muted-foreground">
            {formatCreditsRaw(row.credits_consumed)} 分
          </div>
        </td>
        <td className="hidden p-3 text-right tabular-nums sm:table-cell">
          {durationText(row.duration_ms)}
        </td>
        <td className="whitespace-nowrap p-3 text-right">
          <Button variant="ghost" size="sm" onClick={onToggle}>
            {open ? "收起" : "详情"}
          </Button>
        </td>
      </tr>
      {open ? (
        <tr className="border-t border-border bg-muted/20">
          <td colSpan={8} className="p-3">
            <RunDetail row={row} onChanged={onChanged} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

/**
 * 单条运行的详情。
 *
 * 详情走的是用户侧的 ``GET /api/agent-runs/{id}`` —— 后端的归属校验是
 * 「admin 可看任意 run」，所以管理员能直接拿到 steps / approvals / graph /
 * plan，不必再抄一份 admin 端点。事件与命令用 admin 端点（分页、含审计状态）。
 */
function RunDetail({ row, onChanged }: { row: AdminAgentRunRow; onChanged: () => void }) {
  const detailQ = useQuery({
    queryKey: ["admin-agent-run-detail", row.id],
    queryFn: () => api.getAgentRun(row.id),
    refetchInterval: (query) =>
      query.state.data?.status && TERMINAL.includes(query.state.data.status) ? false : 5000,
  });
  const commandsQ = useQuery({
    queryKey: ["admin-agent-run-commands", row.id],
    queryFn: () => api.adminListRunCommands(row.id),
  });

  const run = detailQ.data;
  const graph = run?.graph ? coerceGraph(row.id, run.graph) : null;
  const plan = run?.plan as
    | {
        summary?: string;
        steps?: Array<{ id: string; title: string; description?: string; sources?: string[] }>;
        acceptanceCriteria?: string[];
      }
    | null
    | undefined;
  const planStatus = run?.plan_status ?? row.plan_status;

  const approveMut = useMutation({
    mutationFn: (approvalId: string) => api.approveToolCall(row.id, approvalId),
    onSuccess: () => {
      toast.success("已批准该工具调用");
      refetchAll();
    },
    onError: (err) => toast.error("批准失败", { description: userErrorMessage(err) }),
  });
  const rejectMut = useMutation({
    mutationFn: (approvalId: string) => api.rejectToolCall(row.id, approvalId, "管理员拒绝"),
    onSuccess: () => {
      toast.success("已拒绝该工具调用");
      refetchAll();
    },
    onError: (err) => toast.error("拒绝失败", { description: userErrorMessage(err) }),
  });
  const cancelMut = useMutation({
    mutationFn: () => api.cancelAgentRun(row.id),
    onSuccess: (result) => {
      // 后端对终态运行也回 200：如实转达，不写死「已取消」。
      toast[result.status === "cancelled" ? "success" : "warning"](
        result.message || `运行状态：${result.status}`
      );
      refetchAll();
    },
    onError: (err) => toast.error("取消失败", { description: userErrorMessage(err) }),
  });

  const refetchAll = () => {
    void detailQ.refetch();
    void commandsQ.refetch();
    onChanged();
  };

  if (detailQ.isError) {
    return <ErrorState onRetry={() => detailQ.refetch()} />;
  }
  if (!run) {
    return <p className="p-3 text-sm text-muted-foreground">详情加载中…</p>;
  }

  const pending = (run.approvals ?? []).filter((a) => a.status === "pending");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>运行 {run.id}</span>
        <span aria-hidden>·</span>
        <span>计划状态：{planStatus || "—"}</span>
        <span aria-hidden>·</span>
        <span>开始 {timeText(run.started_at)}</span>
        <span aria-hidden>·</span>
        <span>结束 {timeText(run.finished_at)}</span>
        {run.error_message ? (
          <span className="text-destructive">错误：{run.error_message}</span>
        ) : null}
        <Link
          href={withConversationParam(new URLSearchParams(), run.conversation_id)}
          className="ml-auto underline-offset-2 hover:underline"
        >
          打开会话
        </Link>
      </div>

      {plan && (plan.summary || (plan.steps && plan.steps.length > 0)) ? (
        <div className="space-y-2">
          <PlanReview
            runId={run.id}
            summary={plan.summary ?? ""}
            steps={plan.steps ?? []}
            acceptanceCriteria={plan.acceptanceCriteria}
            status={planStatus ?? undefined}
            gating={Boolean(run.gate_armed) || Boolean(run.paused_at)}
            onApprove={(id) => api.confirmPlan(id).finally(() => onChanged())}
            onRevise={(id, rev) => api.updatePlan(id, rev).finally(() => onChanged())}
          />
          <PlanGateControl
            runId={run.id}
            armed={Boolean(run.gate_armed)}
            runStatus={run.status}
            planStatus={planStatus ?? undefined}
            onGate={(id, enabled) => api.setPlanGate(id, enabled)}
            onDecided={() => void detailQ.refetch()}
          />
        </div>
      ) : row.plan_present ? (
        <p className="text-xs text-muted-foreground">
          该运行保存了计划，但不是可审阅的结构（可能是引擎内部快照）。
        </p>
      ) : null}

      {pending.length > 0 ? (
        <div className="space-y-2">
          <SectionTitle>待批准的工具调用（{pending.length}）</SectionTitle>
          {pending.map((a) => (
            <ApprovalCard
              key={a.id}
              approval={{
                runId: run.id,
                approvalId: a.id,
                toolName: a.tool_name,
                summary: a.reason || `参数：${JSON.stringify(a.arguments ?? {}).slice(0, 200)}`,
                riskLevel: a.risk_level,
                argumentsPreview: a.arguments ?? {},
              }}
              onApprove={async (id) => {
                await approveMut.mutateAsync(id);
              }}
              onReject={async (id) => {
                await rejectMut.mutateAsync(id);
              }}
            />
          ))}
        </div>
      ) : null}

      {!TERMINAL.includes(run.status) ? (
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="text-destructive"
            disabled={cancelMut.isPending}
            onClick={() => {
              if (confirm(`取消运行 ${run.id}？正在执行的步骤会收到停止信号。`)) {
                cancelMut.mutate();
              }
            }}
          >
            {cancelMut.isPending ? "取消中…" : "取消运行"}
          </Button>
          <span className="text-xs text-muted-foreground">
            取消只作用于未结束的运行；终态行点了也不会改历史。
          </span>
        </div>
      ) : null}

      <Tabs defaultValue="steps">
        <TabsList>
          <TabsTrigger value="steps">步骤时间线</TabsTrigger>
          <TabsTrigger value="graph">执行图</TabsTrigger>
          <TabsTrigger value="events">事件</TabsTrigger>
          <TabsTrigger value="commands">控制命令</TabsTrigger>
        </TabsList>

        <TabsContent value="steps" className="space-y-1">
          {(run.steps ?? []).length === 0 ? (
            <p className="py-4 text-center text-xs text-muted-foreground">
              该运行没有落库步骤。
            </p>
          ) : (
            (run.steps ?? []).map((s) => (
              <div
                key={s.id}
                className="flex items-start gap-2 rounded-md border border-border bg-card px-2 py-1.5 text-xs"
              >
                <span className="w-8 shrink-0 tabular-nums text-muted-foreground">
                  #{s.sequence}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="font-medium">{s.agent_name || s.step_type}</span>
                  {s.tool_name ? (
                    <span className="ml-1 font-mono text-muted-foreground">{s.tool_name}</span>
                  ) : null}
                  <div className="text-muted-foreground">{timeText(s.created_at)}</div>
                </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {s.latency_ms != null ? `${s.latency_ms} ms` : "—"}
                </span>
                <Badge variant={s.status === "completed" ? "secondary" : "outline"} className="shrink-0 text-[10px]">
                  {s.status}
                </Badge>
              </div>
            ))
          )}
        </TabsContent>

        <TabsContent value="graph" className="space-y-3">
          {graph && graph.nodes.length > 0 ? (
            <>
              <AgentRunHeader graph={graph} />
              <AgentFlowGraph nodes={graph.nodes} edges={graph.edges} />
              <AgentActivityFeed graph={graph} />
            </>
          ) : (
            <p className="py-4 text-center text-xs text-muted-foreground">
              该运行没有多 Agent 图快照（单 Agent / 原生运行不落图）。
            </p>
          )}
        </TabsContent>

        <TabsContent value="events">
          <EventsPanel runId={row.id} />
        </TabsContent>

        <TabsContent value="commands" className="space-y-1">
          {commandsQ.isLoading ? (
            <p className="py-4 text-center text-xs text-muted-foreground">加载中…</p>
          ) : !(commandsQ.data ?? []).length ? (
            <p className="py-4 text-center text-xs text-muted-foreground">
              该运行没有收到任何持久控制命令。
            </p>
          ) : (
            (commandsQ.data ?? []).map((c: AdminRunCommandRow) => (
              <div
                key={c.id}
                className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5 text-xs"
              >
                <Badge variant={c.status === "applied" ? "secondary" : "outline"} className="text-[10px]">
                  {COMMAND_TEXT[c.command_type] ?? c.command_type}
                </Badge>
                <span className="text-muted-foreground">
                  {COMMAND_STATUS_TEXT[c.status] ?? c.status}
                </span>
                <span className="font-mono text-muted-foreground">
                  {JSON.stringify(c.payload ?? {})}
                </span>
                <span className="ml-auto text-muted-foreground">
                  {timeText(c.created_at)} → {c.applied_at ? timeText(c.applied_at) : "未消费"}
                </span>
                {c.error ? <span className="text-destructive">{c.error}</span> : null}
              </div>
            ))
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
      {children}
    </h3>
  );
}

/**
 * 事件时间线：分页读 ``run_events``（审计视图）。
 *
 * 实时跟随仍是用户侧那条 SSE 的职责；管理员要看的是「历史上到底发过什么」，
 * 所以要能翻到最早的一条，而不是只看到最后 200 行。
 */
function EventsPanel({ runId }: { runId: string }) {
  const [page, setPage] = useState(0);
  const limit = 30;
  const eventsQ = useQuery({
    queryKey: ["admin-agent-run-events", runId, page],
    queryFn: () => api.adminListRunEvents(runId, limit, page * limit),
  });
  const total = eventsQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <div className="space-y-2">
      {eventsQ.isError ? (
        <ErrorState onRetry={() => eventsQ.refetch()} />
      ) : (
        <div className="max-h-[45vh] space-y-1 overflow-auto">
          {(eventsQ.data?.items ?? []).length === 0 ? (
            <p className="py-4 text-center text-xs text-muted-foreground">
              该运行没有落库事件。
            </p>
          ) : (
            (eventsQ.data?.items ?? []).map((e: AdminRunEventRow) => (
              <div
                key={e.id}
                className="flex items-start gap-2 rounded-md border border-border bg-card px-2 py-1.5 text-xs"
              >
                <span className="w-10 shrink-0 tabular-nums text-muted-foreground">
                  {e.sequence}
                </span>
                <span className="shrink-0 font-medium">{e.event_type}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground">
                  {JSON.stringify(e.data ?? {})}
                </span>
                <span className="shrink-0 text-muted-foreground">{timeText(e.created_at)}</span>
              </div>
            ))
          )}
        </div>
      )}
      <div className="flex items-center justify-between">
        <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
          上一页
        </Button>
        <span className="text-xs text-muted-foreground">
          共 {total} 条事件 · 第 {Math.min(page + 1, pages)} / {pages} 页
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={(page + 1) * limit >= total}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

"use client";

// 用量报表面板（`GET /api/admin/usage`）。
//
// 分组、聚合与分页都在 SQL 里做完（`usage_report`，
// `backend/app/services/admin_service.py:152`）。因此这里不做任何累加：
// 响应里的 `totals` 是**整个区间**的合计，与这一页装了哪几行无关，
// 一旦改成前端按本页求和，运营就会把「本页成本」读成「区间成本」。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Loader2 } from "lucide-react";
import { toast } from "sonner";

import {
  api,
  type AdminUsageGroupBy,
  type AdminUsageMetrics,
  type AdminUsageRow,
} from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { downloadCsvFile, type CsvValue } from "@/lib/csv";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/**
 * 数字与后端同源，注释里写清后端在哪个 file:line。
 *
 * - 每页 100 行 / `limit` 上界 500：`USAGE_MAX_LIMIT`（`backend/app/services/admin_service.py:36`）；
 * - 区间上界 366 天：`USAGE_MAX_RANGE_DAYS`（`backend/app/services/admin_service.py:33`）；
 * - 两个日期都不填时后端补最近 30 天：`USAGE_DEFAULT_DAYS`（`backend/app/services/admin_service.py:34`），
 *   生效区间以响应里的 `start` / `end` 为准，前端不自己算「今天」。
 */
const USAGE_PAGE_SIZE = 100;
const USAGE_MAX_RANGE_DAYS = 366;
/** 导出按 `limit` 上界翻页（同 `USAGE_MAX_LIMIT`），少几轮同样的筛选条件。 */
const EXPORT_PAGE_SIZE = 500;
/** 一次导出最多 1 万行：超了就提示改小区间，而不是让浏览器攒一份全表。 */
const EXPORT_MAX_PAGES = 20;

interface GroupOption {
  value: AdminUsageGroupBy;
  label: string;
  /** 表格与 CSV 的第一列表头。 */
  column: string;
}

const GROUP_OPTIONS: readonly GroupOption[] = [
  { value: "day", label: "按天", column: "日期(UTC)" },
  { value: "model", label: "按模型", column: "模型" },
  { value: "user", label: "按用户", column: "用户" },
];

const METRIC_HEADERS = [
  "消息数",
  "用户消息",
  "请求数",
  "Prompt tokens",
  "Completion tokens",
  "Total tokens",
  "成本 USD",
] as const;

function formatInt(value: number): string {
  return Math.round(Number(value) || 0).toLocaleString("zh-CN");
}

/** 成本按 4 位小数展示，与运行时观测页同一口径（`src/app/admin/runtime/page.tsx:324`）。 */
function formatUsd(value: number): string {
  return `$${(Number(value) || 0).toFixed(4)}`;
}

/** `YYYY-MM-DD` 相减：按 UTC 零点解析，否则跨夏令时地区会差一天。 */
function rangeInDays(start: string, end: string): number | null {
  const from = Date.parse(`${start}T00:00:00Z`);
  const to = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  return Math.round((to - from) / 86_400_000) + 1;
}

function metricsOf(metrics: AdminUsageMetrics): CsvValue[] {
  return [
    metrics.messages,
    metrics.user_messages,
    metrics.requests,
    metrics.prompt_tokens,
    metrics.completion_tokens,
    metrics.total_tokens,
    metrics.cost_usd,
  ];
}

export function UsageReportPanel() {
  const [groupBy, setGroupBy] = useState<AdminUsageGroupBy>("day");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [page, setPage] = useState(0);
  const [exporting, setExporting] = useState(false);

  // 就地挡住明显越界的区间：后端也会拒绝（同一个上界），但没必要为此发一次请求。
  const rangeError = (() => {
    if (!start || !end) return null;
    const days = rangeInDays(start, end);
    if (days === null) return "日期格式不正确";
    if (days < 1) return "开始日期不能晚于结束日期";
    if (days > USAGE_MAX_RANGE_DAYS) return `日期区间最多 ${USAGE_MAX_RANGE_DAYS} 天`;
    return null;
  })();

  const query = {
    start: start || undefined,
    end: end || undefined,
    groupBy,
  };

  const reportQ = useQuery({
    queryKey: ["admin-usage", groupBy, start || null, end || null, page],
    queryFn: () =>
      api.adminUsageReport({
        ...query,
        limit: USAGE_PAGE_SIZE,
        offset: page * USAGE_PAGE_SIZE,
      }),
    enabled: !rangeError,
    placeholderData: (prev) => prev,
  });

  const group = GROUP_OPTIONS.find((g) => g.value === groupBy) ?? GROUP_OPTIONS[0];
  const data = reportQ.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / USAGE_PAGE_SIZE));

  const exportCsv = async () => {
    if (rangeError || exporting) return;
    setExporting(true);
    try {
      const collected: AdminUsageRow[] = [];
      let resolvedStart = "";
      let resolvedEnd = "";
      let totalCount = 0;
      for (let index = 0; index < EXPORT_MAX_PAGES; index += 1) {
        const chunk = await api.adminUsageReport({
          ...query,
          limit: EXPORT_PAGE_SIZE,
          offset: collected.length,
        });
        resolvedStart = chunk.start;
        resolvedEnd = chunk.end;
        totalCount = chunk.total;
        collected.push(...chunk.items);
        if (!chunk.items.length || collected.length >= totalCount) break;
      }
      const headers = [
        group.column,
        ...(groupBy === "user" ? ["邮箱", "用户名"] : []),
        ...METRIC_HEADERS,
      ];
      downloadCsvFile(
        `用量报表-${resolvedStart}至${resolvedEnd}-${group.label}.csv`,
        headers,
        collected.map((row) => [
          row.label,
          ...(groupBy === "user" ? [row.email, row.username] : []),
          ...metricsOf(row),
        ])
      );
      if (collected.length < totalCount) {
        toast.warning(`已导出前 ${collected.length} 行`, {
          description: `本次共有 ${totalCount} 个分组，超过 ${
            EXPORT_PAGE_SIZE * EXPORT_MAX_PAGES
          } 行的部分没有导出，请缩小区间分批导出。`,
        });
      } else {
        toast.success(`已导出 ${collected.length} 行（${resolvedStart} 至 ${resolvedEnd}）`);
      }
    } catch (err) {
      toast.error("导出失败", { description: userErrorMessage(err) });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor="usage-start" className="text-xs text-muted-foreground">
            开始日期（UTC，含当天）
          </Label>
          <Input
            id="usage-start"
            type="date"
            value={start}
            max={end || undefined}
            className="max-w-44"
            onChange={(e) => {
              setStart(e.target.value);
              setPage(0);
            }}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="usage-end" className="text-xs text-muted-foreground">
            结束日期（UTC，含当天）
          </Label>
          <Input
            id="usage-end"
            type="date"
            value={end}
            min={start || undefined}
            className="max-w-44"
            onChange={(e) => {
              setEnd(e.target.value);
              setPage(0);
            }}
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">分组</Label>
          <Select
            value={groupBy}
            onValueChange={(value) => {
              setGroupBy(value as AdminUsageGroupBy);
              setPage(0);
            }}
          >
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {GROUP_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {start || end ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setStart("");
                setEnd("");
                setPage(0);
              }}
            >
              恢复默认区间
            </Button>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            disabled={!!rangeError || exporting || reportQ.isFetching}
            onClick={() => void exportCsv()}
          >
            {exporting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5" />
            )}
            导出 CSV
          </Button>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        {rangeError ? (
          <span className="text-destructive">{rangeError}</span>
        ) : (
          <>
            区间 {data ? `${data.start} 至 ${data.end}` : "（读取中）"} · 按 UTC
            日历日统计，两端都含当天；两个日期都留空时由后端给默认区间。最长{" "}
            {USAGE_MAX_RANGE_DAYS} 天，「请求」按 assistant 消息计数，token
            与成本也只记在这些行上。
          </>
        )}
      </p>

      {reportQ.isError ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-10 text-center">
          <p className="text-sm text-muted-foreground">
            用量数据加载失败：{userErrorMessage(reportQ.error)}
          </p>
          <Button variant="outline" size="sm" onClick={() => reportQ.refetch()}>
            重试
          </Button>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">{group.column}</th>
                <th className="p-3 text-right">消息</th>
                <th className="hidden p-3 text-right sm:table-cell">用户消息</th>
                <th className="p-3 text-right">请求</th>
                <th className="hidden p-3 text-right md:table-cell">Prompt</th>
                <th className="hidden p-3 text-right md:table-cell">Completion</th>
                <th className="p-3 text-right">Total tokens</th>
                <th className="p-3 text-right">成本</th>
              </tr>
            </thead>
            <tbody>
              {reportQ.isLoading ? (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !items.length ? (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-muted-foreground">
                    该区间内没有消息记录。
                  </td>
                </tr>
              ) : (
                items.map((row) => (
                  <tr key={row.key} className="border-t border-border">
                    <td className="p-3 font-medium">
                      {row.label}
                      {groupBy === "user" && row.email ? (
                        <div className="text-xs font-normal text-muted-foreground">
                          {row.email}
                        </div>
                      ) : null}
                    </td>
                    <td className="p-3 text-right tabular-nums">{formatInt(row.messages)}</td>
                    <td className="hidden p-3 text-right tabular-nums sm:table-cell">
                      {formatInt(row.user_messages)}
                    </td>
                    <td className="p-3 text-right tabular-nums">{formatInt(row.requests)}</td>
                    <td className="hidden p-3 text-right tabular-nums md:table-cell">
                      {formatInt(row.prompt_tokens)}
                    </td>
                    <td className="hidden p-3 text-right tabular-nums md:table-cell">
                      {formatInt(row.completion_tokens)}
                    </td>
                    <td className="p-3 text-right tabular-nums">
                      {formatInt(row.total_tokens)}
                    </td>
                    <td className="p-3 text-right tabular-nums">{formatUsd(row.cost_usd)}</td>
                  </tr>
                ))
              )}
            </tbody>
            {data?.totals ? (
              <tfoot className="border-t border-border bg-secondary/30 text-xs">
                <tr>
                  <td className="p-3 text-muted-foreground">区间合计（{total} 个分组）</td>
                  <td className="p-3 text-right tabular-nums">
                    {formatInt(data.totals.messages)}
                  </td>
                  <td className="hidden p-3 text-right tabular-nums sm:table-cell">
                    {formatInt(data.totals.user_messages)}
                  </td>
                  <td className="p-3 text-right tabular-nums">{formatInt(data.totals.requests)}</td>
                  <td className="hidden p-3 text-right tabular-nums md:table-cell">
                    {formatInt(data.totals.prompt_tokens)}
                  </td>
                  <td className="hidden p-3 text-right tabular-nums md:table-cell">
                    {formatInt(data.totals.completion_tokens)}
                  </td>
                  <td className="p-3 text-right tabular-nums">{formatInt(data.totals.total_tokens)}</td>
                  <td className="p-3 text-right tabular-nums">{formatUsd(data.totals.cost_usd)}</td>
                </tr>
              </tfoot>
            ) : null}
          </table>
        </div>
      )}

      <div className="flex items-center justify-between">
        <Button
          variant="outline"
          size="sm"
          disabled={page === 0 || reportQ.isFetching}
          onClick={() => setPage((p) => Math.max(0, p - 1))}
        >
          上一页
        </Button>
        <span className="text-xs text-muted-foreground">
          第 {Math.min(page + 1, pages)} / {pages} 页
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={(page + 1) * USAGE_PAGE_SIZE >= total || reportQ.isFetching}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

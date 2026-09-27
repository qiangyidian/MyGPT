"use client";

// 审计日志面板（`GET /api/admin/audit`）。
//
// 筛选与分页都在服务端做（`backend/app/api/admin.py:161`）：一旦分页，浏览器手上
// 只有这一页，在客户端过滤就变成「只在这一页里找」，看上去没结果其实有。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Loader2, Search } from "lucide-react";
import { toast } from "sonner";

import {
  api,
  type AdminAuditQuery,
  type AdminAuditRow,
} from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { downloadCsvFile, type CsvValue } from "@/lib/csv";
// SQLite 取回的时间是 naive UTC，`new Date(str)` 会按本地时区读它：解析走全项目那一个入口。
import { formatDateTimeText } from "@/lib/redeem-batch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * 每页 50 / 上界 500：`AUDIT_DEFAULT_LIMIT` 与 `AUDIT_MAX_LIMIT`
 * （`backend/app/api/admin.py:35` 与 `:34`）。
 */
const AUDIT_PAGE_SIZE = 50;
const AUDIT_MAX_LIMIT = 500;
/** 一次导出最多 500 × 20 页 = 1 万条，超了提示改条件而不是让浏览器攒全表。 */
const EXPORT_MAX_PAGES = 20;

interface AuditFilters {
  action: string;
  actionPrefix: string;
  actor: string;
  q: string;
  start: string;
  end: string;
}

const EMPTY_FILTERS: AuditFilters = {
  action: "",
  actionPrefix: "",
  actor: "",
  q: "",
  start: "",
  end: "",
};

const FIELDS: {
  key: keyof AuditFilters;
  label: string;
  placeholder: string;
  hint: string;
}[] = [
  {
    key: "action",
    label: "操作（完整匹配）",
    placeholder: "credits:adjust",
    hint: "填 action 的确切值",
  },
  {
    key: "actionPrefix",
    label: "操作前缀",
    placeholder: "credits:",
    hint: "按前缀筛一类操作",
  },
  {
    key: "actor",
    label: "操作人",
    placeholder: "邮箱、用户名或用户 id",
    hint: "id 精确匹配，邮箱 / 用户名模糊匹配",
  },
  {
    key: "q",
    label: "关键字（匹配目标）",
    placeholder: "会话 id、用户 id 等",
    hint: "在「目标」里找包含关系",
  },
];

function toQuery(filters: AuditFilters, limit: number, offset: number): AdminAuditQuery {
  return {
    action: filters.action.trim() || undefined,
    actionPrefix: filters.actionPrefix.trim() || undefined,
    actor: filters.actor.trim() || undefined,
    q: filters.q.trim() || undefined,
    start: filters.start || undefined,
    end: filters.end || undefined,
    limit,
    offset,
  };
}

function detailText(row: AdminAuditRow): string {
  return row.detail ? JSON.stringify(row.detail) : "";
}

export function AuditLogPanel() {
  const [draft, setDraft] = useState<AuditFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<AuditFilters>(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [exporting, setExporting] = useState(false);

  // 后端也会拒（同一个校验），但没必要为一次明显反了的区间发请求。
  const rangeError =
    applied.start && applied.end && applied.start > applied.end
      ? "开始日期不能晚于结束日期"
      : null;
  const filtered = Object.values(applied).some((value) => !!value);

  const auditQ = useQuery({
    queryKey: ["admin-audit", applied, page],
    queryFn: () => api.adminAuditLog(toQuery(applied, AUDIT_PAGE_SIZE, page * AUDIT_PAGE_SIZE)),
    enabled: !rangeError,
    placeholderData: (prev) => prev,
  });

  const items = auditQ.data?.items ?? [];
  const total = auditQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / AUDIT_PAGE_SIZE));

  const setField = (key: keyof AuditFilters, value: string) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
    // 日期是点选的，改了就该立刻生效；文本框要等「查询」，否则每个字发一次请求。
    if (key === "start" || key === "end") {
      setApplied((prev) => ({ ...prev, [key]: value }));
      setPage(0);
    }
  };

  const submit = () => {
    setApplied(draft);
    setPage(0);
  };

  const reset = () => {
    setDraft(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setPage(0);
  };

  const exportCsv = async () => {
    if (rangeError || exporting) return;
    setExporting(true);
    try {
      const collected: AdminAuditRow[] = [];
      let totalCount = 0;
      for (let index = 0; index < EXPORT_MAX_PAGES; index += 1) {
        const chunk = await api.adminAuditLog(
          toQuery(applied, AUDIT_MAX_LIMIT, collected.length)
        );
        totalCount = chunk.total;
        collected.push(...chunk.items);
        if (!chunk.items.length || collected.length >= totalCount) break;
      }
      downloadCsvFile(
        `审计日志-${applied.start || "全部"}至${applied.end || "现在"}.csv`,
        ["时间(UTC)", "操作人邮箱", "操作人用户名", "操作", "目标", "详情"],
        collected.map(
          (row): CsvValue[] => [
            row.created_at ?? "",
            row.actor_email ?? "",
            row.actor_username ?? "",
            row.action,
            row.target ?? "",
            detailText(row),
          ]
        )
      );
      if (collected.length < totalCount) {
        toast.warning(`已导出前 ${collected.length} 条`, {
          description: `当前条件共有 ${totalCount} 条，超出部分没有导出，请缩小日期区间或加筛选条件分批导出。`,
        });
      } else {
        toast.success(`已导出 ${collected.length} 条`);
      }
    } catch (err) {
      toast.error("导出失败", { description: userErrorMessage(err) });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-3">
      <form
        className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {FIELDS.map((field) => (
          <div key={field.key} className="space-y-1">
            <Label htmlFor={`audit-${field.key}`} className="text-xs text-muted-foreground">
              {field.label}
            </Label>
            <Input
              id={`audit-${field.key}`}
              value={draft[field.key]}
              placeholder={field.placeholder}
              onChange={(e) => setField(field.key, e.target.value)}
            />
            <p className="text-[11px] text-muted-foreground">{field.hint}</p>
          </div>
        ))}

        <div className="space-y-1">
          <Label htmlFor="audit-start" className="text-xs text-muted-foreground">
            开始日期（UTC，含当天）
          </Label>
          <Input
            id="audit-start"
            type="date"
            value={draft.start}
            max={draft.end || undefined}
            onChange={(e) => setField("start", e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="audit-end" className="text-xs text-muted-foreground">
            结束日期（UTC，含当天）
          </Label>
          <Input
            id="audit-end"
            type="date"
            value={draft.end}
            min={draft.start || undefined}
            onChange={(e) => setField("end", e.target.value)}
          />
        </div>

        <div className="flex flex-wrap items-end gap-2 sm:col-span-2 lg:col-span-3">
          <Button type="submit" size="sm" className="gap-1.5">
            <Search className="h-3.5 w-3.5" />
            查询
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={reset}>
            清空条件
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="ml-auto gap-1.5"
            disabled={!!rangeError || exporting || auditQ.isFetching}
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
      </form>

      {rangeError ? <p className="text-xs text-destructive">{rangeError}</p> : null}

      <p className="text-xs text-muted-foreground">
        共 {total} 条 · 第 {Math.min(page + 1, pages)} / {pages} 页 · 本页{" "}
        {items.length} 条。日期按 UTC 日历日筛，两端都含当天；表格里的时间是浏览器本地时区，
        导出的 CSV 是 UTC 原值。
      </p>

      {auditQ.isError ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-10 text-center">
          <p className="text-sm text-muted-foreground">
            审计日志加载失败：{userErrorMessage(auditQ.error)}
          </p>
          <Button variant="outline" size="sm" onClick={() => auditQ.refetch()}>
            重试
          </Button>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="whitespace-nowrap p-3">时间</th>
                <th className="hidden p-3 md:table-cell">操作人</th>
                <th className="p-3">操作</th>
                <th className="hidden max-w-[180px] p-3 sm:table-cell">目标</th>
                <th className="hidden max-w-[280px] p-3 lg:table-cell">详情</th>
              </tr>
            </thead>
            <tbody>
              {auditQ.isLoading ? (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !items.length ? (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-muted-foreground">
                    {filtered ? "没有符合当前条件的审计事件。" : "还没有审计事件。"}
                  </td>
                </tr>
              ) : (
                items.map((row) => (
                  <tr key={row.id} className="border-t border-border align-top">
                    <td className="whitespace-nowrap p-3 text-muted-foreground tabular-nums">
                      {formatDateTimeText(row.created_at)}
                    </td>
                    <td className="hidden p-3 text-muted-foreground md:table-cell">
                      {row.actor_email || row.actor_username || "（无操作人）"}
                    </td>
                    <td className="p-3 font-medium">{row.action}</td>
                    <td
                      className="hidden max-w-[180px] truncate p-3 text-muted-foreground sm:table-cell"
                      title={row.target ?? ""}
                    >
                      {row.target ?? "—"}
                    </td>
                    <td
                      className="hidden max-w-[280px] truncate p-3 font-mono text-xs text-muted-foreground lg:table-cell"
                      title={detailText(row)}
                    >
                      {detailText(row) || "—"}
                    </td>
                  </tr>
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
          disabled={page === 0 || auditQ.isFetching}
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
          disabled={(page + 1) * AUDIT_PAGE_SIZE >= total || auditQ.isFetching}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import type { RedeemCodeRow } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const PAGE_SIZE = 20;

// 状态过滤器：只列后端认识的三个字面值（``redeem_codes.status``）。
// 传别的会被服务端 400，所以这里不给自由输入。
const STATUS_FILTERS: { value: string | null; label: string }[] = [
  { value: null, label: "全部" },
  { value: "active", label: "未兑换" },
  { value: "redeemed", label: "已兑换" },
  { value: "void", label: "已作废" },
];

const STATUS_TEXT: Record<string, { label: string; destructive?: boolean }> = {
  active: { label: "未兑换" },
  redeemed: { label: "已兑换" },
  void: { label: "已作废", destructive: true },
};

/**
 * 单码看板：批次行点「查看单码」进来。
 *
 * 明文在这里是**取不到**的 —— 库里只有 peppered HMAC，掩码只给 6 位前缀。
 * 所以「reveal」这个动作不存在：能做的只有拿前缀去和用户手上那张对一下。
 * 与其放一个假的显示按钮，不如把这句话写在界面上。
 *
 * 作废与删除都只碰未兑换的码；服务端用条件更新 + 409 表达「这张刚被别人处理」，
 * 这里把 409 当作刷新信号而不是终局失败。
 */
export function RedeemCodeBrowser({
  batch,
  onClose,
}: {
  batch: { id: string; name: string } | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [page, setPage] = useState(0);

  const codesQ = useQuery({
    queryKey: ["admin-redeem-codes", batch?.id ?? null, statusFilter, page],
    queryFn: () =>
      api.adminQueryRedeemCodes({
        batchId: batch?.id ?? null,
        status: statusFilter,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    enabled: !!batch,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["admin-redeem-codes"] });
    // 单码状态变了会影响批次的「剩 N / 作废 M」计数。
    qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
  };

  const runAction = useMutation({
    mutationFn: (args: { id: string; kind: "void" | "delete" }) =>
      args.kind === "void"
        ? api.adminVoidRedeemCode(args.id)
        : api.adminDeleteRedeemCode(args.id),
    onSuccess: (result) => {
      toast[result.changed ? "success" : "warning"](
        result.changed ? result.message : `${result.message}（状态未变化）`
      );
      invalidate();
    },
    onError: (err) => {
      toast.error("操作失败", { description: userErrorMessage(err) });
      // 409 = 乐观锁失败：别人已经改过这张码，列表必须重读才能对齐。
      invalidate();
    },
  });

  const items = codesQ.data?.items ?? [];
  const total = codesQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <Dialog
      open={!!batch}
      onOpenChange={(next) => {
        if (!next) {
          onClose();
          setStatusFilter(null);
          setPage(0);
        }
      }}
    >
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>单码明细 · {batch?.name}</DialogTitle>
          <DialogDescription>
            系统只保存兑换码的哈希，明文在生成后无法再次显示；下面的掩码只用于按前缀
            和用户手上那张对一下。作废与删除都只作用于未兑换的码。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-2">
          {STATUS_FILTERS.map((f) => (
            <Button
              key={f.label}
              size="sm"
              variant={statusFilter === f.value ? "default" : "outline"}
              onClick={() => {
                setStatusFilter(f.value);
                setPage(0);
              }}
            >
              {f.label}
            </Button>
          ))}
          <span className="ml-auto text-xs text-muted-foreground">
            共 {total} 张
          </span>
        </div>

        {codesQ.isError ? (
          <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-10 text-center">
            <p className="text-sm text-muted-foreground">单码列表加载失败，请重试。</p>
            <Button variant="outline" size="sm" onClick={() => codesQ.refetch()}>
              重试
            </Button>
          </div>
        ) : (
          <div className="max-h-[55vh] overflow-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="p-3">兑换码</th>
                  <th className="p-3">状态</th>
                  <th className="hidden p-3 sm:table-cell">面额</th>
                  <th className="p-3">核销人</th>
                  <th className="hidden p-3 md:table-cell">有效期</th>
                  <th className="p-3" />
                </tr>
              </thead>
              <tbody>
                {codesQ.isLoading ? (
                  <tr>
                    <td colSpan={6} className="p-6 text-center text-muted-foreground">
                      加载中…
                    </td>
                  </tr>
                ) : !items.length ? (
                  <tr>
                    <td colSpan={6} className="p-6 text-center text-muted-foreground">
                      该筛选条件下没有兑换码。
                    </td>
                  </tr>
                ) : (
                  items.map((row: RedeemCodeRow) => {
                    const st = STATUS_TEXT[row.status] ?? { label: row.status };
                    return (
                      <tr key={row.id} className="border-t border-border">
                        <td className="whitespace-nowrap p-3 font-mono text-xs">
                          <span className="tracking-wider">{row.masked_code}</span>
                          <button
                            type="button"
                            className="ml-2 text-muted-foreground underline-offset-2 hover:underline"
                            onClick={() => {
                              navigator.clipboard.writeText(row.code_prefix);
                              toast.success("已复制前缀，可让用户报出兑换码前 6 位比对");
                            }}
                          >
                            <Copy className="h-3 w-3" />
                          </button>
                        </td>
                        <td className="p-3">
                          <Badge
                            variant={st.destructive ? "destructive" : "secondary"}
                            className="text-[10px]"
                          >
                            {st.label}
                          </Badge>
                        </td>
                        <td className="hidden p-3 tabular-nums sm:table-cell">
                          {row.credits_per_code}
                        </td>
                        <td className="p-3 text-muted-foreground">
                          {row.redeemed_at
                            ? row.redeemer_email || row.redeemer_username || "（账号已注销）"
                            : "—"}
                          {row.redeemed_at ? (
                            <div className="text-xs">
                              {new Date(row.redeemed_at).toLocaleString()}
                            </div>
                          ) : null}
                        </td>
                        <td className="hidden p-3 text-muted-foreground md:table-cell">
                          {row.expires_at
                            ? new Date(row.expires_at).toLocaleDateString()
                            : "永久"}
                        </td>
                        <td className="whitespace-nowrap p-3 text-right">
                          {row.status === "active" ? (
                            <>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-destructive"
                                disabled={runAction.isPending}
                                onClick={() => {
                                  if (
                                    confirm(
                                      `作废这张码（${row.masked_code}）？作废后它永久无法兑换，用户拿着它会直接得到「该兑换码已作废」，而且没有任何恢复入口 —— 这一步不可撤销。`
                                    )
                                  ) {
                                    runAction.mutate({ id: row.id, kind: "void" });
                                  }
                                }}
                              >
                                作废
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={runAction.isPending}
                                onClick={() => {
                                  if (
                                    confirm(
                                      `删除这张码（${row.masked_code}）？删除会连记录一起消失、不可恢复，批次总数也会随之减少，只在确认这张从未发放出去时才用它；拿不准就改点「作废」（作废保留记录）。已兑换的码后端禁止删除，要留档对账。`
                                    )
                                  ) {
                                    runAction.mutate({ id: row.id, kind: "delete" });
                                  }
                                }}
                              >
                                删除
                              </Button>
                            </>
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              {runAction.isPending ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : null}
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        )}

        <div className="flex items-center justify-between">
          <Button
            variant="outline"
            size="sm"
            disabled={page === 0 || codesQ.isFetching}
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
            disabled={(page + 1) * PAGE_SIZE >= total || codesQ.isFetching}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

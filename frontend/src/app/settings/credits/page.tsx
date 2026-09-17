"use client";

import { useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Coins, Info } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { formatCredits, normalizeRedeemCodeInput, redeemErrorMessage } from "@/lib/credits";
import { useCredits } from "@/hooks/useCredits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

const LEDGER_KEY = ["credits", "ledger"] as const;

/** 流水原因 → 中文标签。 */
const REASON_LABELS: Record<string, string> = {
  redeem: "兑换码",
  admin_adjust: "管理员调整",
  usage: "对话消耗",
  signup_bonus: "注册赠送",
};

/**
 * 积分页：余额、兑换码输入、消费流水。
 *
 * 观察模式下顶部会显示提示条 —— 此时余额不足不会拦截，用户看到扣分却
 * 没被拦会困惑，必须说清楚。
 */
export default function CreditsSettingsPage() {
  const qc = useQueryClient();
  const { credits, isLoading } = useCredits();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const ledgerQ = useInfiniteQuery({
    queryKey: LEDGER_KEY,
    queryFn: ({ pageParam }) => api.fetchCreditLedger(50, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const redeem = useMutation({
    mutationFn: (value: string) => api.redeemCode(value),
    onSuccess: (result) => {
      toast.success(`兑换成功，获得 ${formatCredits(result.credits_added)} 积分`);
      setCode("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["credits"] });
    },
    onError: (err) => {
      const apiErr = err as ApiError;
      setError(redeemErrorMessage(apiErr.code, apiErr.message));
    },
  });

  const submit = () => {
    const normalized = normalizeRedeemCodeInput(code);
    if (normalized.replace(/-/g, "").length !== 16) {
      setError("兑换码应为 16 位字符");
      return;
    }
    redeem.mutate(normalized);
  };

  const entries = ledgerQ.data?.pages.flatMap((p) => p.entries) ?? [];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins className="h-5 w-5" /> 我的积分
          </CardTitle>
          <CardDescription>对话与专家模式按实际模型消耗扣除积分。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
            <div>
              <div className="text-xs text-muted-foreground">当前余额</div>
              <div className="text-3xl font-semibold tabular-nums">
                {isLoading ? "—" : formatCredits(credits?.balance)}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">累计获得</div>
              <div className="text-lg tabular-nums">
                {formatCredits(credits?.lifetime_granted)}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">累计消耗</div>
              <div className="text-lg tabular-nums">
                {formatCredits(credits?.lifetime_consumed)}
              </div>
            </div>
          </div>

          {credits && !credits.enforced ? (
            <div className="flex items-start gap-2 rounded-md border border-border bg-secondary/40 p-3 text-xs text-muted-foreground">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                当前为观察模式：积分会照常扣减与记账，但余额不足时不会中断对话。
              </span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>兑换积分</CardTitle>
          <CardDescription>输入兑换码，积分立即到账。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              value={code}
              onChange={(e) => {
                setCode(normalizeRedeemCodeInput(e.target.value));
                setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
              placeholder="XXXX-XXXX-XXXX-XXXX"
              aria-label="兑换码"
              className="font-mono tracking-wider"
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              onClick={submit}
              disabled={redeem.isPending || !code}
              className="sm:w-28"
            >
              {redeem.isPending ? "兑换中…" : "兑换"}
            </Button>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>消费明细</CardTitle>
          <CardDescription>最近 50 条，按时间倒序。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {ledgerQ.isLoading ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : entries.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无记录。</p>
          ) : (
            <>
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="p-3">时间</th>
                      <th className="p-3">原因</th>
                      <th className="p-3 text-right">变动</th>
                      <th className="p-3 text-right">余额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => (
                      <tr key={entry.id} className="border-t border-border">
                        <td className="whitespace-nowrap p-3 text-muted-foreground">
                          {new Date(entry.created_at).toLocaleString()}
                        </td>
                        <td className="p-3">
                          {REASON_LABELS[entry.reason] ?? entry.reason}
                          {entry.note ? (
                            <span className="ml-2 text-xs text-muted-foreground">
                              {entry.note}
                            </span>
                          ) : null}
                        </td>
                        <td
                          className={
                            entry.delta >= 0
                              ? "p-3 text-right tabular-nums text-emerald-600"
                              : "p-3 text-right tabular-nums text-muted-foreground"
                          }
                        >
                          {entry.delta >= 0 ? `+${entry.delta}` : entry.delta}
                        </td>
                        <td className="p-3 text-right tabular-nums">
                          {entry.balance_after}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {ledgerQ.hasNextPage ? (
                <div className="flex justify-center">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={ledgerQ.isFetchingNextPage}
                    onClick={() => ledgerQ.fetchNextPage()}
                  >
                    {ledgerQ.isFetchingNextPage ? "加载中…" : "加载更多"}
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

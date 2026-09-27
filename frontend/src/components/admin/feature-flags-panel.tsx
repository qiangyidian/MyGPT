"use client";

// 后台「运行开关」（条目 34④）：每个运营开关的**生效结论**。
//
// 原先这里什么都没有：想知道引擎走没走、积分挡不挡，只能 ssh 上去读 `.env`。而
// `.env` 的原文和"实际生效"是两件事 —— 引擎是总开关 AND 灰度名单，`python_exec` 在
// 生产是显式放行 AND 真隔离后端，`/docs` 在生产默认关且与 `DOCS_ENABLED` 的默认值
// 无关。把原文抄给人看，等于让运营在服务器上现场重推一遍判定式。
//
// 这一面只读，是有意的：这些值多数在进程启动时读一次（runner 工厂、策略对象都是启动
// 期构造），界面上改出来的状态和重启后的状态不一致，比不让人改更危险。需要即时生效的
// 是工具粒度的启停，那一面在「工具」Tab。

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import {
  flagStatusText,
  groupFlags,
  inactiveFlags,
} from "@/lib/feature-flags";

export function FeatureFlagsPanel() {
  const flagsQ = useQuery({
    queryKey: ["admin-feature-flags"],
    queryFn: () => api.adminFeatureFlags(),
    // 这份结论来自"回答这一次的那个进程"，多副本滚动发版期间两个副本可以给出不同
    // 答案 —— 所以每次进入都重新问一次，不拿 30 秒前的缓存当现状。
    staleTime: 0,
  });

  if (flagsQ.isError) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-10 text-center">
        <p className="text-sm text-muted-foreground">
          开关状态加载失败：{userErrorMessage(flagsQ.error)}
        </p>
        <Button variant="outline" size="sm" onClick={() => flagsQ.refetch()}>
          重试
        </Button>
      </div>
    );
  }

  const data = flagsQ.data;
  const flags = data?.flags ?? [];
  const groups = groupFlags(flags);
  const inactive = inactiveFlags(flags);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs text-muted-foreground">
          只读。这些值由环境变量决定，改完要重启后端才会一致。
          {data
            ? ` · 由 ${data.env} 环境的进程于 ${new Date(data.generated_at).toLocaleString(
                "zh-CN",
                { timeZone: "UTC", hour12: false }
              )}（UTC）算出`
            : ""}
        </p>
        <Button
          variant="outline"
          size="sm"
          className="gap-1.5"
          disabled={flagsQ.isFetching}
          onClick={() => flagsQ.refetch()}
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新读取
        </Button>
      </div>

      {/* 顶上的提醒只数"没生效"的那些：全绿的时候没人需要一句"一切正常"，而真正会
          踩的坑是「我以为引擎在跑」。 */}
      {data && inactive.length ? (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-muted-foreground">
          当前有 {inactive.length} 项未生效：
          {inactive.map((flag) => flag.label).join("、")}。 每一条都写清了要改哪个变量。
        </p>
      ) : null}

      {flagsQ.isLoading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : (
        groups.map((section) => (
          <div key={section.group} className="space-y-2">
            <h3 className="text-sm font-medium">{section.label}</h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {section.items.map((flag) => (
                <div
                  key={flag.key}
                  className="rounded-lg border border-border bg-card p-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{flag.label}</span>
                    <Badge
                      variant={flag.enabled ? "default" : "secondary"}
                      className="text-[10px]"
                    >
                      {flagStatusText(flag)}
                    </Badge>
                    <code className="ml-auto text-[11px] text-muted-foreground">
                      {flag.value}
                    </code>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{flag.note}</p>
                  <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                    {flag.source}
                  </p>
                </div>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

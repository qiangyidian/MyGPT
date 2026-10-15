"use client";

import { Boxes, Loader2, Settings2 } from "lucide-react";
import { useRouter } from "next/navigation";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { useModels } from "@/hooks/useModels";
import { filterModelsByModality } from "@/lib/multimodal";
import { groupModelsForMenu } from "@/lib/model-groups";
import { cn } from "@/lib/utils";

const DEFAULT_MODEL_VALUE = "__none__";

interface ModelSelectorProps {
  value: string | null;
  onChange: (modelId: string | null) => void;
  className?: string;
  /**
   * Attachment mime types currently in the composer. When provided, the menu
   * filters to models that support every requested modality (image → vision,
   * audio → audio_input). Undefined / empty → all chat models (default path).
   */
  mimes?: string[];
}

/**
 * 两级模型选择器（供应商 → 模型），图二式级联菜单：
 *
 * 第一级是用户命名的模型配置（按 name 分组，见 lib/model-groups），第二级才是
 * 具体模型。顶部保留「默认模型」——选它（null）等于让后端自己挑；底部入口
 * 跳设置页的「管理模型」。mimes 过滤逻辑与旧 Select 版本完全一致。
 *
 * 外观交给调用方：唯一消费者是输入框工具栏的设置组，组内每一项都不带自己的
 * 边框与圆角（边框归整个组），所以 className 默认被沿用而不在此硬编码。
 */
export function ModelSelector({
  value,
  onChange,
  className,
  mimes,
}: ModelSelectorProps) {
  const { chatModels, isLoading } = useModels();
  const router = useRouter();

  const groupSource =
    mimes && mimes.length > 0 ? filterModelsByModality(chatModels, mimes) : chatModels;
  const groups = groupModelsForMenu(groupSource);

  const selected = chatModels.find((m) => m.id === value);
  const triggerLabel = value === null ? "默认模型" : selected
    ? selected.name
    : "选择模型";

  if (isLoading) {
    // 组内的一项，所以不带自己的边框：一个转圈 + 一句短文案就够，别在组里
    // 再画一个矩形出来。
    return (
      <div
        className={cn(
          "flex h-9 items-center gap-1.5 px-2.5 text-sm text-muted-foreground",
          className
        )}
      >
        <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
        <span className="truncate">加载模型…</span>
      </div>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn("h-9 max-w-[220px] gap-1.5 text-sm font-medium", className)}
          title={`模型：${triggerLabel}`}
        >
          <Boxes className="h-4 w-4 shrink-0" />
          <span className="truncate">{triggerLabel}</span>
        </Button>
      </DropdownMenuTrigger>
      {/* Composer sits at the bottom of the chat column — always open upward. */}
      <DropdownMenuContent align="start" side="top" className="w-[240px]">
        <DropdownMenuItem
          onSelect={() => onChange(null)}
          className={cn(value === null && "bg-accent")}
        >
          <span className={cn("min-w-0 flex-1 truncate", value === null && "font-semibold")}>
            默认模型
          </span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-[11px] text-muted-foreground">
          按供应商
        </DropdownMenuLabel>
        {groups.map((group) => (
          <DropdownMenuSub key={group.providerName}>
            <DropdownMenuSubTrigger className="gap-2">
              <span className="min-w-0 flex-1 truncate">{group.providerName}</span>
              <span className="text-[11px] text-muted-foreground">
                {group.models.length}
              </span>
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent className="max-h-[300px] overflow-y-auto">
              {group.models.map((leaf) => {
                const active = leaf.id === value;
                return (
                  <DropdownMenuItem
                    key={leaf.id}
                    onSelect={() => onChange(leaf.id)}
                    className={cn(active && "bg-accent")}
                  >
                    <span
                      className={cn(
                        "min-w-0 flex-1 truncate",
                        active && "font-semibold"
                      )}
                    >
                      {leaf.displayName}
                    </span>
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => router.push("/settings/models")}>
          <Settings2 className="mr-2 h-4 w-4" />
          管理模型
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

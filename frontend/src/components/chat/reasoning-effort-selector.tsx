"use client";

import { Gauge } from "lucide-react";

import { cn } from "@/lib/utils";
import { useModels } from "@/hooks/useModels";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * 三档的中文名是给用户看的；括号里的 wire 值只在菜单里当脚注，**不上触发器**
 * ——触发器上写「均衡（medium）」等于把枚举摆到产品界面上，既长又是噪声。
 */
const EFFORTS = [
  { value: "low", label: "快速", hint: "low" },
  { value: "medium", label: "均衡", hint: "medium" },
  { value: "high", label: "深入", hint: "high" },
] as const;

const DEFAULT_EFFORT = "medium";

/**
 * Reasoning-effort picker (B6): shown when the selected model (or, when
 * "默认模型" is chosen, ANY chat model) declares supports_reasoning_effort.
 * The value rides the chat request as `reasoning_effort`; the backend passes
 * it to the provider only for capable models and ignores it otherwise.
 *
 * 用 DropdownMenu 而不是 Select：Select 的触发器自带 w-full 与 justify-between，
 * 放进设置组会独占一整行、并把文字顶到中间；而且 SSR 下它把值渲染成一个空
 * span（值要等 hydration 才出现）。菜单式与组内另两项同族，行为与外观都一致。
 */
export function ReasoningEffortSelector({
  modelId,
  value,
  onChange,
  className,
}: {
  modelId: string | null;
  value: "low" | "medium" | "high" | undefined;
  onChange: (v: "low" | "medium" | "high") => void;
  className?: string;
}) {
  const { chatModels } = useModels();

  const capable = modelId
    ? chatModels.some((m) => m.id === modelId && m.supports_reasoning_effort)
    : chatModels.some((m) => m.supports_reasoning_effort);
  if (!capable) return null;

  const current = value ?? DEFAULT_EFFORT;
  const meta = EFFORTS.find((e) => e.value === current) ?? EFFORTS[1];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={className}
          title={`推理力度：${meta.label}`}
          aria-label={`推理力度：${meta.label}`}
        >
          <Gauge className="h-4 w-4 shrink-0" />
          <span className="truncate">{meta.label}</span>
        </Button>
      </DropdownMenuTrigger>
      {/* Composer sits at the bottom of the chat column — always open upward. */}
      <DropdownMenuContent align="start" side="top" className="w-[200px]">
        <DropdownMenuLabel className="text-[11px] font-normal text-muted-foreground">
          推理力度
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {EFFORTS.map((e) => {
          const active = e.value === current;
          return (
            <DropdownMenuItem
              key={e.value}
              onSelect={() => onChange(e.value)}
              className={cn(active && "bg-accent")}
            >
              <span className={cn("min-w-0 flex-1 truncate", active && "font-semibold")}>
                {e.label}
              </span>
              <span className="text-[11px] text-muted-foreground">{e.hint}</span>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

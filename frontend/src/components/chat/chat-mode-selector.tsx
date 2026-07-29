"use client";

import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useChatUiStore } from "@/stores/chat-ui-store";
import { USER_MODES } from "@/lib/user-modes";
import type { UserChatMode } from "@/lib/types";

/**
 * The single user-facing capability picker. Shows friendly labels and short
 * descriptions; the selected value is the stable ``UserChatMode`` wire enum the
 * backend IntentRouter consumes. Internal runtime names (Native/CrewAI) are
 * never exposed here.
 */
export function ChatModeSelector({ className }: { className?: string }) {
  const mode = useChatUiStore((s) => s.mode);
  const setMode = useChatUiStore((s) => s.setMode);
  const meta = USER_MODES.find((m) => m.value === mode) ?? USER_MODES[0];
  const Icon = meta.icon;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={cn("h-9 gap-1.5 text-sm font-medium", className)}
          aria-label={`当前模式：${meta.label}。点击切换`}
        >
          <Icon className="h-4 w-4" />
          <span>{meta.short}</span>
          <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72">
        <DropdownMenuLabel className="text-xs text-muted-foreground">
          选择能力
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {USER_MODES.map((m) => {
          const MIcon = m.icon;
          const active = m.value === mode;
          return (
            <DropdownMenuItem
              key={m.value}
              className="flex cursor-pointer items-start gap-2.5 py-2"
              onClick={() => setMode(m.value as UserChatMode)}
            >
              <MIcon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 text-sm font-medium">
                  {m.label}
                  {active && <Check className="h-3.5 w-3.5 text-primary" />}
                </div>
                <p className="text-xs leading-snug text-muted-foreground">
                  {m.description}
                </p>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

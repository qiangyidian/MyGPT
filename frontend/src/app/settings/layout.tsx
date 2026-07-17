"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode } from "react";
import { Boxes, Cpu, ChevronLeft } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

// Settings sections. Currently only "Models" is wired up; the rest are
// placeholders so the nav reflects the intended structure.
const NAV = [
  {
    label: "模型配置",
    href: "/settings/models",
    icon: Cpu,
    description: "管理 OpenAI 兼容 / Mock 模型",
    enabled: true,
  },
  {
    label: "知识库",
    href: "/settings/knowledge",
    icon: Boxes,
    description: "管理向量知识库与文档",
    enabled: false,
  },
] as const;

export default function SettingsLayout({
  children,
}: {
  children: ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="min-h-screen bg-muted/30">
      <div className="container flex max-w-6xl flex-col gap-6 py-8 lg:flex-row">
        {/* Sidebar */}
        <aside className="lg:w-64 lg:shrink-0">
          <div className="sticky top-8 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold tracking-tight">设置</h2>
              <Button
                asChild
                variant="ghost"
                size="sm"
                className="text-muted-foreground"
              >
                <Link href="/">
                  <ChevronLeft className="mr-1" />
                  返回
                </Link>
              </Button>
            </div>
            <p className="text-sm text-muted-foreground">
              管理你的模型、知识库等配置。
            </p>

            <nav className="space-y-1">
              {NAV.map((item) => {
                const active =
                  pathname === item.href || pathname.startsWith(item.href + "/");
                const Icon = item.icon;
                const inner = (
                  <span
                    className={cn(
                      "flex items-start gap-3 rounded-md border px-3 py-2.5 text-sm transition-colors",
                      active
                        ? "border-border bg-background text-foreground shadow-sm"
                        : "border-transparent text-muted-foreground hover:bg-background/60 hover:text-foreground",
                      !item.enabled && "pointer-events-none opacity-50"
                    )}
                  >
                    <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                    <span className="flex flex-col">
                      <span className="font-medium">{item.label}</span>
                      <span className="text-xs text-muted-foreground">
                        {item.description}
                      </span>
                    </span>
                  </span>
                );
                return item.enabled ? (
                  <Link key={item.href} href={item.href}>
                    {inner}
                  </Link>
                ) : (
                  <div key={item.href} title="即将上线">
                    {inner}
                  </div>
                );
              })}
            </nav>
          </div>
        </aside>

        {/* Content */}
        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}

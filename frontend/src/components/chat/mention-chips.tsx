"use client";

// 正文里已经落子的 ``@`` 引用（条目 2）。
//
// 引用是长在文本里的 token（见 lib/inline-refs.ts），所以这里没有第二份状态要
// 同步：token 就是唯一事实，摘掉一枚 = 从正文里删掉那个 token。列出来只是为了
// 让人一眼看清「这一轮到底喂了哪些库/文档/文件」，以及一键删除。

import { X } from "lucide-react";

import { parseRefs, REF_KIND_LABEL, type InlineRef } from "@/lib/inline-refs";
import { cn } from "@/lib/utils";

interface MentionChipsProps {
  /** 编辑器正文（token 的原产地）。 */
  text: string;
  onRemove: (ref: InlineRef) => void;
  className?: string;
}

export function MentionChips({
  text,
  onRemove,
  className,
}: MentionChipsProps) {
  const refs = parseRefs(text);
  if (refs.length === 0) return null;
  return (
    <div
      className={cn("flex flex-wrap items-center gap-1.5", className)}
      aria-label="已引用的目标"
    >
      {refs.map((ref) => (
        <span
          key={`${ref.kind}:${ref.id}`}
          // token 里带的是完整名字，长文件名要能看出来是被截断的。
          title={`${REF_KIND_LABEL[ref.kind]}：${ref.label}`}
          className="bg-muted text-muted-foreground flex max-w-[220px] items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]"
        >
          <span className="shrink-0">{REF_KIND_LABEL[ref.kind]}</span>
          <span className="text-foreground truncate">{ref.label}</span>
          <button
            type="button"
            onClick={() => onRemove(ref)}
            aria-label={`移除引用 ${ref.label}`}
            className="hover:text-foreground shrink-0 rounded-full p-0.5"
          >
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}
    </div>
  );
}

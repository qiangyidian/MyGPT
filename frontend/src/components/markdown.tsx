"use client";

import { memo, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Copy-to-clipboard button for code blocks. Rendered inside the dark code-block
 * header bar, so it is always visible and tuned for a dark surface.
 */
function CodeCopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard may be unavailable */
    }
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      className="inline-flex items-center gap-1 rounded text-[11px] text-zinc-400 transition-colors hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400/60"
      aria-label={copied ? "已复制" : "复制代码"}
    >
      {copied ? (
        <Check className="h-3 w-3 text-green-400" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
      {copied ? "已复制" : "复制"}
    </button>
  );
}

/**
 * Markdown renderer with GFM, syntax highlighting, and copy buttons on
 * code blocks. Safe by default — ReactMarkdown does not allow raw HTML
 * unless rehype-raw is added.
 *
 * Styling is done via explicit Tailwind utilities targeting markdown
 * elements (no @tailwindcss/typography dependency).
 */
export const Markdown = memo(function Markdown({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "max-w-none break-words text-sm leading-relaxed text-foreground",
        // Paragraphs.
        "[&_p]:my-2 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0",
        // Headings.
        "[&_h1]:mt-4 [&_h1]:mb-2 [&_h1]:text-xl [&_h1]:font-semibold",
        "[&_h2]:mt-4 [&_h2]:mb-2 [&_h2]:text-lg [&_h2]:font-semibold",
        "[&_h3]:mt-3 [&_h3]:mb-1.5 [&_h3]:text-base [&_h3]:font-semibold",
        "[&_h4]:mt-3 [&_h4]:mb-1.5 [&_h4]:text-sm [&_h4]:font-semibold",
        "[&_h5]:mt-2 [&_h5]:mb-1 [&_h5]:text-sm [&_h5]:font-medium",
        "[&_h6]:mt-2 [&_h6]:mb-1 [&_h6]:text-sm [&_h6]:font-medium",
        // Lists.
        "[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5",
        "[&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5",
        "[&_li]:my-0.5",
        // Code blocks (background / rounding / margin come from the pre renderer wrapper).
        "[&_pre]:my-0 [&_pre]:overflow-x-auto [&_pre]:bg-transparent [&_pre]:p-4",
        "[&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-[13px] [&_pre_code]:leading-relaxed",
        // Inline code.
        "[&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.85em] [&_code]:font-mono",
        "[&_pre_code]:bg-transparent",
        // Links.
        "[&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2",
        // Blockquotes.
        "[&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-4 [&_blockquote]:italic [&_blockquote]:text-muted-foreground",
        // Tables.
        "[&_table]:my-3 [&_table]:w-full [&_table]:border-collapse [&_table]:text-xs",
        "[&_th]:border [&_th]:border-border [&_th]:bg-muted [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold",
        "[&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1",
        // Horizontal rule.
        "[&_hr]:my-4 [&_hr]:border-border",
        // Images.
        "[&_img]:max-w-full [&_img]:rounded-md",
        className
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          pre({ children }) {
            // Extract raw text (for the copy button) and the language (for the
            // header label) from the child <code> element. rehype-highlight
            // marks it up as <code class="hljs language-x">.
            let text = "";
            let lang = "";
            const extract = (node: ReactNode): void => {
              if (typeof node === "string") {
                text += node;
              } else if (Array.isArray(node)) {
                node.forEach(extract);
              } else if (node && typeof node === "object") {
                const el = node as {
                  props?: { children?: ReactNode; className?: string };
                  type?: string;
                };
                if (
                  el.type === "code" &&
                  !lang &&
                  typeof el.props?.className === "string"
                ) {
                  const match = el.props.className.match(/language-([\w-]+)/);
                  if (match) lang = match[1];
                }
                if (el.props?.children) extract(el.props.children);
              }
            };
            extract(children);

            return (
              <div className="group/code my-4 overflow-hidden rounded-lg border border-black/5 bg-[#0b1021] dark:border-white/10">
                <div className="flex items-center justify-between border-b border-white/10 bg-white/[0.03] px-3 py-1.5">
                  <span className="font-mono text-[11px] lowercase text-zinc-400">
                    {lang || "code"}
                  </span>
                  <CodeCopyButton text={text} />
                </div>
                <pre className="overflow-x-auto p-4">{children}</pre>
              </div>
            );
          },
          a({ href, children }) {
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});

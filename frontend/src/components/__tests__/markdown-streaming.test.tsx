/**
 * Streaming code-fence rendering contract.
 *
 * A code block that is still being streamed (fence opened, not yet closed)
 * must render its partial content — the "code appears only after the block
 * finishes" bug. These tests pin the Markdown component to that behavior
 * via react-markdown's server renderer (same pipeline the browser runs,
 * minus the DOM), so a regression in the fence handling fails here instead
 * of in front of a user.
 */
import { beforeAll, describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import * as React from "react";

import { Markdown } from "@/components/markdown";

// The component file uses classic JSX (no per-file React import); the Next.js
// compiler injects the runtime, vitest does not.
beforeAll(() => {
  (globalThis as { React?: unknown }).React = React;
});

const midFence = "回答如下：\n\n```python\nprint(1)\nprint(2)";
const fenceHeaderOnly = "回答如下：\n\n```python\n";

describe("Markdown streaming fences", () => {
  it("renders partial code inside an unclosed fence (lite/streaming mode)", () => {
    const html = renderToString(
      React.createElement(Markdown, { content: midFence, lite: true })
    );
    expect(html).toContain("print(1)");
    expect(html).toContain("print(2)");
    // Rendered as a code block (the custom pre wrapper), not inline text.
    expect(html).toContain("language-python");
    // Regression (2026-09-05): in lite mode rehype-highlight does NOT run, so
    // no .hljs class applies the light base color — the code inherited
    // text-foreground (near-black in light mode) and was invisible on the
    // dark block until streaming completed. The wrapper must force the light
    // base color on <pre> whether or not highlighting has run. (The rendered
    // class attribute is HTML-escaped: `&` → `&amp;`.)
    expect(html).toContain("[&amp;_pre]:text-[#e6e6e6]");
  });

  it("shows the language label while the fence header just arrived", () => {
    const html = renderToString(
      React.createElement(Markdown, { content: fenceHeaderOnly, lite: true })
    );
    expect(html).toContain("python");
  });

  it("renders closed fences unchanged (regression guard)", () => {
    const html = renderToString(
      React.createElement(Markdown, {
        content: "```python\nprint('done')\n```",
        lite: true,
      })
    );
    // HTML-escaped quotes: the assertion uses the escaped form.
    expect(html).toContain("print(&#x27;done&#x27;)");
  });

  it("renders code streaming mid-line inside an unclosed fence", () => {
    // The exact states a stream passes through while a code block is being
    // written: fence header → first line → mid-line → many lines.
    const states = [
      "```python\n",
      "```python\nprint",
      "```python\nprint(1",
      "```python\nprint(1)\nprint(2)\nfor i in range(10)",
    ];
    for (const content of states) {
      const html = renderToString(
        React.createElement(Markdown, { content, lite: true })
      );
      const body = content.replace("```python\n", "");
      if (body) {
        // The last partial line must be visible (in escaped form).
        expect(html).toContain(escapeHtml(body.slice(-12)));
      }
    }
  });

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}
});

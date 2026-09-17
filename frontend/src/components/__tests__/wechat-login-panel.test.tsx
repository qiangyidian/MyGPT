/**
 * The "公众号验证码" panel: scan the Official Account, then type the code it
 * replied with.
 *
 * Rendered through react-dom/server (same pipeline the browser runs, minus the
 * DOM) so these assertions pin the behaviour a user actually sees: the QR guide
 * when the deployment has one, the text fallback when it does not, and a submit
 * button that cannot fire on an incomplete code.
 */
import { beforeAll, describe, expect, it, vi } from "vitest";
import { renderToString } from "react-dom/server";
import * as React from "react";

import { WechatLoginPanel } from "@/components/wechat-login-panel";
import type { WechatLoginInfo } from "@/lib/types";

// The component file uses classic JSX (no per-file React import); the Next.js
// compiler injects the runtime, vitest does not.
beforeAll(() => {
  (globalThis as { React?: unknown }).React = React;
});

const CONFIGURED: WechatLoginInfo = {
  configured: true,
  qrcode_url: "/images/wechat-account-qrcode.jpg",
  keyword: "验证码",
};

const UNCONFIGURED: WechatLoginInfo = {
  configured: false,
  qrcode_url: "",
  keyword: "验证码",
};

function render(overrides: Partial<Parameters<typeof WechatLoginPanel>[0]> = {}) {
  const props = {
    info: CONFIGURED,
    code: "",
    onCodeChange: vi.fn(),
    onSubmit: vi.fn(),
    loading: false,
    error: null,
    ...overrides,
  };
  return { html: renderToString(React.createElement(WechatLoginPanel, props)), props };
}

/** Whether the submit button is actually disabled.
 *
 * A substring search for "disabled" is ALWAYS true here: the Label primitive
 * carries `peer-disabled:*` classes and the Button primitive carries
 * `disabled:pointer-events-none`. Matching the rendered attribute (React emits
 * `disabled=""` for a true boolean attr) is the only honest check — a
 * substring version passes no matter what the button does.
 */
function submitDisabled(html: string): boolean {
  const match = html.match(/<button[^>]*>/);
  if (!match) throw new Error("no submit button rendered");
  return / disabled=""/.test(match[0]);
}

describe("WechatLoginPanel", () => {
  it("shows the QR image and the three-step guide when configured", () => {
    const { html } = render();
    expect(html).toContain('src="/images/wechat-account-qrcode.jpg"');
    expect(html).toContain("微信扫描");
    expect(html).toContain("关注");
    // Tells an already-following user how to get a code: they never trigger the
    // subscribe event, so the keyword is their only route.
    expect(html).toContain("验证码");
  });

  it("falls back to a text hint with no QR configured", () => {
    // A broken <img> plus instructions that reference it would be worse than
    // no image at all, so the panel must drop the image AND the scan step.
    const { html } = render({ info: UNCONFIGURED });
    expect(html).not.toContain("<img");
    expect(html).toContain("微信公众号");
  });

  it("falls back to a text hint before login-info has loaded", () => {
    const { html } = render({ info: null });
    expect(html).not.toContain("<img");
    expect(html).toContain("6 位登录验证码");
  });

  it("uses the configured keyword in the instructions", () => {
    const { html } = render({ info: { ...CONFIGURED, keyword: "取码" } });
    expect(html).toContain("取码");
  });

  it("constrains the input to six digits", () => {
    // The account only ever sends digits; a stray space or IME artifact would
    // otherwise surface as a confusing "验证码错误".
    const { html } = render();
    expect(html).toContain('maxLength="6"');
    expect(html).toContain('inputMode="numeric"');
  });

  it("blocks submit until a full code is typed", () => {
    expect(submitDisabled(render({ code: "" }).html)).toBe(true);
    expect(submitDisabled(render({ code: "12345" }).html)).toBe(true);
    // Six digits = submittable.
    expect(submitDisabled(render({ code: "123456" }).html)).toBe(false);
  });

  it("stays disabled while a submit is in flight", () => {
    // Guards double-submission: a second press would burn a second code.
    expect(submitDisabled(render({ code: "123456", loading: true }).html)).toBe(true);
  });

  it("enables submit exactly at six characters", () => {
    // Off-by-one on the boundary is the easy mistake here.
    for (const code of ["1", "12345", "123456", "1234567"]) {
      expect(submitDisabled(render({ code }).html)).toBe(code.length !== 6);
    }
  });

  it("renders the error it is given", () => {
    const { html } = render({ error: "公众号验证码错误或已过期" });
    expect(html).toContain("公众号验证码错误或已过期");
  });

  it("uses a context-appropriate submit label", () => {
    expect(render({ submitLabel: "绑定微信", code: "123456" }).html).toContain("绑定微信");
  });
});

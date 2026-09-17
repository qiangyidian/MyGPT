"use client";

import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { WechatLoginInfo } from "@/lib/types";

/**
 * The "公众号验证码" panel: scan the Official Account, then type the 6-digit
 * code it replies with.
 *
 * Split out of the login page (and reused by the account-settings binding
 * card) so the instructions and input behaviour are not maintained twice, and
 * so the rendered output can be asserted on without a DOM.
 *
 * The flow is deliberately NOT "scan and you are in": WeChat's passive reply
 * carries the code and the user types it back. That indirection is exactly what
 * lets one scan work on every service sharing the Official Account.
 */
export function WechatLoginPanel({
  info,
  code,
  onCodeChange,
  onSubmit,
  loading,
  error,
  submitLabel = "登录",
}: {
  info: WechatLoginInfo | null;
  code: string;
  onCodeChange: (value: string) => void;
  onSubmit: () => void;
  loading: boolean;
  error?: string | null;
  submitLabel?: string;
}) {
  const keyword = info?.keyword || "验证码";
  const hasQr = Boolean(info?.configured && info.qrcode_url);
  const qrUrl = info?.qrcode_url ?? "";

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      {hasQr ? (
        <div className="flex flex-col items-center gap-3" data-testid="wechat-login-guide">
          {/* Served from the deployment's own public/ dir: same-origin, so no
              hotlink or referer problem. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={qrUrl}
            alt="微信公众号二维码"
            width={160}
            height={160}
            data-testid="wechat-login-qrcode"
            className="rounded-md border bg-white p-1"
          />
          <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
            <li>使用微信扫描上方二维码，关注公众号</li>
            <li>
              关注后会自动收到 6 位登录验证码（已关注的用户，在公众号发送「
              {keyword}」重新获取）
            </li>
            <li>在下方输入验证码完成登录</li>
          </ol>
        </div>
      ) : (
        // No QR configured: degrade to instructions rather than show a broken
        // image with a scan step the user cannot perform.
        <p
          className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
          data-testid="wechat-login-unconfigured"
        >
          请在微信公众号发送「{keyword}」获取 6 位登录验证码，在下方输入后即可登录。
        </p>
      )}

      <div className="space-y-2">
        <Label htmlFor="wechat-code">公众号验证码</Label>
        <Input
          id="wechat-code"
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="6 位数字"
          maxLength={6}
          value={code}
          // Digits only: the account never sends anything else, and a stray
          // space or IME artifact would surface as a confusing 401.
          onChange={(e) => onCodeChange(e.target.value.replace(/\D/g, "").slice(0, 6))}
          disabled={loading}
        />
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      <Button type="submit" className="w-full" disabled={loading || code.length !== 6}>
        {loading && <Loader2 className="animate-spin" />}
        {submitLabel}
      </Button>
    </form>
  );
}

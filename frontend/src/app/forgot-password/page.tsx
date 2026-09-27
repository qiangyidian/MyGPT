"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, KeyRound, Loader2, MailCheck } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
// 规则常量与中文文案的唯一来源（后端 `app/core/security.py` 的抄本）。
import { passwordProblem, passwordPolicySummary } from "@/lib/password-policy";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PasswordInput } from "@/components/ui/password-input";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

// 找回密码三步：填邮箱 → 输验证码 + 新密码 → 完成。
//
// 两条不能破的规则：
//  1. 密码只进 POST 请求体。整页不读也不写 URL query（邮箱同样只存本地 state），
//     因为 query 会进浏览器历史、Referer 和反向代理日志。
//  2. 第一步的文案与「邮箱是否存在」无关（后端同样恒返回一句话）。这里要是
//     根据结果改成「该邮箱未注册」，防枚举就直接废掉。
type Step = "email" | "code" | "done";

// 后端 EMAIL_CODE_TTL_SECONDS 默认 5 分钟，重发间隔 60 秒。
const RESEND_SECONDS = 60;

function emailProblem(value: string): string | null {
  const mail = value.trim();
  if (!mail) return "请输入邮箱地址";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(mail)) return "邮箱格式不正确";
  return null;
}

/** 限流和账号状态这几类服务端错误在本页有专门语义，其余交给统一映射。 */
function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) {
      return err.message || "操作过于频繁，请稍后再试";
    }
    if (err.status === 401) return "账号已被禁用，请联系管理员";
  }
  return userErrorMessage(err);
}

export default function ForgotPasswordPage() {
  return <ForgotPasswordFlow />;
}

function ForgotPasswordFlow() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [countdown, setCountdown] = useState(0);

  useEffect(() => {
    if (countdown <= 0) return;
    const t = window.setInterval(() => {
      setCountdown((c) => (c <= 1 ? 0 : c - 1));
    }, 1000);
    return () => window.clearInterval(t);
  }, [countdown]);

  const sendCode = async (mail: string) => {
    const problem = emailProblem(mail);
    if (problem) {
      setError(problem);
      return false;
    }
    setPending(true);
    setError(null);
    setHint(null);
    try {
      const res = await api.requestPasswordReset(mail.trim());
      setCountdown(RESEND_SECONDS);
      setHint(res.message);
      return true;
    } catch (err) {
      setError(errorText(err));
      return false;
    } finally {
      setPending(false);
    }
  };

  async function handleRequest(e: FormEvent) {
    e.preventDefault();
    if (await sendCode(email)) setStep("code");
  }

  async function handleResend() {
    await sendCode(email);
  }

  async function handleReset(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!/^\d{6}$/.test(code.trim())) {
      setError("请输入邮箱收到的 6 位验证码");
      return;
    }
    const problem = passwordProblem(newPwd);
    if (problem) {
      setError(problem);
      return;
    }
    if (newPwd !== confirmPwd) {
      setError("两次输入的新密码不一致");
      return;
    }
    setPending(true);
    try {
      await api.resetPasswordWithEmailCode(email.trim(), code.trim(), newPwd);
      setStep("done");
    } catch (err) {
      setError(errorText(err));
      // 验证码是一次性的：服务端说无效就把输入清掉，避免拿旧码反复重试。
      setCode("");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-b from-muted/40 to-background px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <KeyRound className="h-6 w-6" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">找回密码</h1>
          <p className="text-sm text-muted-foreground">
            用注册邮箱接收验证码，并重设登录密码
          </p>
        </div>

        <Card>
          <CardHeader className="space-y-1 pb-4">
            <CardTitle className="text-xl">
              {step === "email" ? "第 1 步 · 验证邮箱" : step === "code" ? "第 2 步 · 设置新密码" : "重置完成"}
            </CardTitle>
            <CardDescription>
              {step === "email"
                ? "输入注册邮箱，我们会发送 6 位重置验证码（5 分钟内有效）。"
                : step === "code"
                  ? "验证码 5 分钟内有效，只能使用一次。重置成功后所有设备的旧登录状态都会失效。"
                  : "请用新密码重新登录。"}
            </CardDescription>
          </CardHeader>

          <CardContent className="pt-0">
            {step === "email" && (
              <form onSubmit={handleRequest} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="fp-email">邮箱</Label>
                  <Input
                    id="fp-email"
                    type="email"
                    autoComplete="email"
                    placeholder="you@example.com"
                    value={email}
                    onChange={(e) => {
                      setEmail(e.target.value);
                      setError(null);
                    }}
                    disabled={pending}
                  />
                </div>
                {error && <ErrorBox message={error} />}
                <Button type="submit" className="w-full" disabled={pending}>
                  {pending && <Loader2 className="animate-spin" />}
                  发送验证码
                </Button>
              </form>
            )}

            {step === "code" && (
              <form onSubmit={handleReset} className="space-y-4">
                {hint && (
                  <div className="flex items-start gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm">
                    <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                    <span>{hint}</span>
                  </div>
                )}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label htmlFor="fp-code">邮箱验证码</Label>
                    <button
                      type="button"
                      className="text-xs text-muted-foreground underline-offset-2 hover:underline disabled:opacity-50"
                      disabled={pending || countdown > 0}
                      onClick={() => void handleResend()}
                    >
                      {countdown > 0 ? `${countdown}s 后可重发` : "重新发送"}
                    </button>
                  </div>
                  <div className="flex gap-2">
                    <Input
                      id="fp-code"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      placeholder="6 位数字"
                      maxLength={6}
                      value={code}
                      onChange={(e) => {
                        setCode(e.target.value.replace(/\D/g, "").slice(0, 6));
                        setError(null);
                      }}
                      disabled={pending}
                      className="flex-1"
                    />
                    <span className="flex h-10 items-center whitespace-nowrap rounded-md border bg-muted/40 px-3 text-xs text-muted-foreground">
                      {email.trim() || "未填写邮箱"}
                    </span>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="fp-new">新密码</Label>
                  <PasswordInput
                    id="fp-new"
                    autoComplete="new-password"
                    placeholder={passwordPolicySummary()}
                    value={newPwd}
                    onChange={(e) => {
                      setNewPwd(e.target.value);
                      setError(null);
                    }}
                    disabled={pending}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="fp-confirm">确认新密码</Label>
                  <PasswordInput
                    id="fp-confirm"
                    autoComplete="new-password"
                    placeholder="再次输入新密码"
                    value={confirmPwd}
                    onChange={(e) => {
                      setConfirmPwd(e.target.value);
                      setError(null);
                    }}
                    disabled={pending}
                  />
                </div>

                {error && <ErrorBox message={error} />}

                <Button
                  type="submit"
                  className="w-full"
                  disabled={pending || !code || !newPwd || !confirmPwd}
                >
                  {pending && <Loader2 className="animate-spin" />}
                  {pending ? "提交中…" : "重置密码"}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  className="w-full"
                  disabled={pending}
                  onClick={() => {
                    setStep("email");
                    setError(null);
                    setHint(null);
                    setCode("");
                  }}
                >
                  <ArrowLeft className="h-4 w-4" />
                  换个邮箱
                </Button>
              </form>
            )}

            {step === "done" && (
              <div className="space-y-4">
                <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm">
                  密码已重置，请用新密码登录。
                </div>
                <Button className="w-full" onClick={() => router.replace("/login")}>
                  前往登录
                </Button>
              </div>
            )}
          </CardContent>

          <CardFooter className="flex flex-col gap-1 border-t bg-muted/30 py-3">
            <p className="text-center text-xs text-muted-foreground">
              收不到验证码？检查垃圾箱，或确认邮箱是否为注册时填写的地址。
            </p>
            <p className="text-center text-xs text-muted-foreground">
              忘记密码通常是公众号扫码注册的账号 —— 那类账号没有密码，直接在
              <Link
                href="/login"
                className="mx-1 text-primary underline-offset-2 hover:underline"
              >
                登录页
              </Link>
              用公众号验证码登录即可。
            </p>
          </CardFooter>
        </Card>
      </div>
    </div>
  );
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
      {message}
    </div>
  );
}

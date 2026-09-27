"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";
import { Loader2, MessageSquare } from "lucide-react";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
// 注册分支的密码规则：常量与中文文案的唯一来源（后端 `app/core/security.py` 的抄本）。
// 登录分支不套它 —— 后端 `LoginRequest.password` 刻意没有长度规则。
import { passwordProblem, passwordPolicySummary } from "@/lib/password-policy";
import { resolveReturnTo } from "@/lib/navigation";
import { useAuth } from "@/hooks/useAuth";
import { NavSuspense } from "@/components/navigation/page-loading";
import { WechatLoginPanel } from "@/components/wechat-login-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PasswordInput } from "@/components/ui/password-input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import type { WechatLoginInfo } from "@/lib/types";

type Mode = "login" | "register" | "wechat";

export default function LoginPage() {
  return (
    <NavSuspense>
      <LoginForm />
    </NavSuspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoading: authLoading } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Shared fields
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  // Register-only fields
  const [username, setUsername] = useState("");
  const [confirm, setConfirm] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [codeSending, setCodeSending] = useState(false);
  const [codeCountdown, setCodeCountdown] = useState(0);

  // WeChat Official Account tab. `wechatInfo` stays null until login-info
  // answers; the panel renders its text fallback for null, so an unreachable
  // or unconfigured deployment never shows a broken QR.
  const [wechatCode, setWechatCode] = useState("");
  const [wechatInfo, setWechatInfo] = useState<WechatLoginInfo | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .fetchWechatLoginInfo()
      .then((info) => {
        if (!cancelled) setWechatInfo(info);
      })
      .catch(() => {
        // Not fatal: the email/password tabs still work, and the panel already
        // renders the no-QR fallback.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Countdown for the send-code button (60s resend interval mirrors the backend).
  useEffect(() => {
    if (codeCountdown <= 0) return;
    const t = window.setInterval(() => {
      setCodeCountdown((c) => (c <= 1 ? 0 : c - 1));
    }, 1000);
    return () => window.clearInterval(t);
  }, [codeCountdown]);

  async function handleSendCode() {
    const mail = email.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(mail)) {
      setError("请先输入合法的邮箱地址，再发送验证码");
      return;
    }
    setCodeSending(true);
    setError(null);
    try {
      const res = await api.requestEmailCode(mail);
      setCodeCountdown(60);
      if (res.debug_code) {
        // Dev mode without SMTP: show the echoed code directly.
        toast.info(`开发环境验证码：${res.debug_code}`);
      } else {
        toast.success("验证码已发送，请查收邮箱（注意垃圾箱）");
      }
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setCodeSending(false);
    }
  }

  // Destination after a successful login/register: a validated `next` param, or "/".
  const next = resolveReturnTo(searchParams, "/");

  // Already authenticated? Skip straight to the destination.
  useEffect(() => {
    if (authLoading || !user) return;
    router.replace(next);
  }, [authLoading, user, next, router]);

  function resetError() {
    if (error) setError(null);
  }

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !password) {
      setError("请输入邮箱和密码");
      return;
    }
    setLoading(true);
    try {
      const { user } = await api.login(email.trim(), password);
      toast.success(`欢迎回来，${user.username}`);
      router.replace(next);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleRegister(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !username.trim() || !password) {
      setError("请填写邮箱、用户名和密码");
      return;
    }
    if (!/^\d{6}$/.test(verificationCode.trim())) {
      setError("请输入邮箱验证码（点击「发送验证码」获取）");
      return;
    }
    const problem = passwordProblem(password);
    if (problem) {
      setError(problem);
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setLoading(true);
    try {
      await api.register(email.trim(), username.trim(), password, verificationCode.trim());
      // Auto-login after register for smoother UX.
      const { user } = await api.login(email.trim(), password);
      toast.success(`注册成功，欢迎 ${user.username}`);
      router.replace(next);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleWechatLogin() {
    setError(null);
    if (!/^\d{6}$/.test(wechatCode.trim())) {
      setError("请输入公众号回复的 6 位验证码");
      return;
    }
    setLoading(true);
    try {
      const { user } = await api.loginWithWechatCode(wechatCode.trim());
      toast.success(`欢迎，${user.username}`);
      router.replace(next);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-b from-muted/40 to-background px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <MessageSquare className="h-6 w-6" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">AI 对话平台</h1>
          <p className="text-sm text-muted-foreground">
            登录或注册以开始你的对话
          </p>
        </div>

        <Card>
          <CardHeader className="space-y-1 pb-4">
            <CardTitle className="text-xl">账号</CardTitle>
            <CardDescription>
              选择登录已有账号，或注册一个新账号
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <Tabs
              value={mode}
              onValueChange={(v) => {
                setMode(v as Mode);
                resetError();
              }}
            >
              <TabsList className="grid w-full grid-cols-3">
                <TabsTrigger value="login">登录</TabsTrigger>
                <TabsTrigger value="register">注册</TabsTrigger>
                <TabsTrigger value="wechat">公众号验证码</TabsTrigger>
              </TabsList>

              {/* ---------------- Login ---------------- */}
              <TabsContent value="login" className="mt-4">
                <form onSubmit={handleLogin} className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="login-email">邮箱</Label>
                    <Input
                      id="login-email"
                      type="email"
                      autoComplete="email"
                      placeholder="you@example.com"
                      value={email}
                      onChange={(e) => {
                        setEmail(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <Label htmlFor="login-password">密码</Label>
                      <Link
                        href="/forgot-password"
                        className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                      >
                        忘记密码？
                      </Link>
                    </div>
                    <PasswordInput
                      id="login-password"
                      autoComplete="current-password"
                      placeholder="••••••"
                      value={password}
                      onChange={(e) => {
                        setPassword(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>

                  {error && <ErrorBox message={error} />}

                  <Button type="submit" className="w-full" disabled={loading}>
                    {loading && <Loader2 className="animate-spin" />}
                    登录
                  </Button>
                </form>
              </TabsContent>

              {/* ---------------- Register ---------------- */}
              <TabsContent value="register" className="mt-4">
                <form onSubmit={handleRegister} className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="reg-email">邮箱</Label>
                    <Input
                      id="reg-email"
                      type="email"
                      autoComplete="email"
                      placeholder="you@example.com"
                      value={email}
                      onChange={(e) => {
                        setEmail(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-code">邮箱验证码</Label>
                    <div className="flex gap-2">
                      <Input
                        id="reg-code"
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        placeholder="6 位数字"
                        maxLength={6}
                        value={verificationCode}
                        onChange={(e) => {
                          setVerificationCode(e.target.value.replace(/\D/g, "").slice(0, 6));
                          resetError();
                        }}
                        disabled={loading}
                        className="flex-1"
                      />
                      <Button
                        type="button"
                        variant="outline"
                        className="w-28 shrink-0"
                        disabled={loading || codeSending || codeCountdown > 0}
                        onClick={() => void handleSendCode()}
                      >
                        {codeSending ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : codeCountdown > 0 ? (
                          `${codeCountdown}s 后重发`
                        ) : (
                          "发送验证码"
                        )}
                      </Button>
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-username">用户名</Label>
                    <Input
                      id="reg-username"
                      autoComplete="username"
                      placeholder="your-name"
                      value={username}
                      onChange={(e) => {
                        setUsername(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-password">密码</Label>
                    <PasswordInput
                      id="reg-password"
                      autoComplete="new-password"
                      placeholder={passwordPolicySummary()}
                      value={password}
                      onChange={(e) => {
                        setPassword(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-confirm">确认密码</Label>
                    <PasswordInput
                      id="reg-confirm"
                      autoComplete="new-password"
                      placeholder="再次输入密码"
                      value={confirm}
                      onChange={(e) => {
                        setConfirm(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>

                  {error && <ErrorBox message={error} />}

                  <Button type="submit" className="w-full" disabled={loading}>
                    {loading && <Loader2 className="animate-spin" />}
                    注册并登录
                  </Button>
                </form>
              </TabsContent>

              {/* ---------------- WeChat Official Account ---------------- */}
              <TabsContent value="wechat" className="mt-4">
                <WechatLoginPanel
                  info={wechatInfo}
                  code={wechatCode}
                  onCodeChange={(v) => {
                    setWechatCode(v);
                    resetError();
                  }}
                  onSubmit={() => void handleWechatLogin()}
                  loading={loading}
                  error={error}
                />
              </TabsContent>
            </Tabs>
          </CardContent>
          <CardFooter className="flex flex-col gap-2 border-t bg-muted/30 py-3">
            <p className="text-center text-xs text-muted-foreground">
              首次部署请联系管理员创建账号，或在注册页注册新账号。
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

// 英文/内部信息一律交给统一映射：后端自己的中文优先，未收录的英文不会漏给用户。
const toMessage = userErrorMessage;
